use crate::pyjson::{self, Dump, Json};
use crate::{Result, common::*, py};
use num_bigint::BigInt;
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

const STEM: &str = "[a-z0-9]+(?:_[a-z0-9]+)*";
const PREFIX: &str = "[a-z0-9]+(?:-[a-z0-9]+)*";
const MANIFEST: &str = "release-manifest.json";
const CHECKSUMS: &str = "SHA256SUMS";
const MAX_METADATA: u64 = 1024 * 1024;
const MAX_ASSET: u64 = 512 * 1024 * 1024;
const POLICY_REPOSITORY: &str = "ryanduguid/release-policy";
const POLICY_WORKFLOW: &str = ".github/workflows/release-python.yml";

#[derive(Clone, Debug, PartialEq)]
pub struct ReleaseTag {
    pub version: String,
    pub version_tag: String,
    pub full_tag: String,
    pub artifact_tag: String,
    pub tag_prefix: String,
}

pub fn validate_tag_prefix(prefix: &str) -> Result<()> {
    if !prefix.is_empty() && !full(PREFIX, prefix) {
        return Err("tag-prefix must be empty or lower-case and hyphenated".into());
    }
    Ok(())
}

pub fn derive_release_tag(version: &str, prefix: &str) -> Result<ReleaseTag> {
    if !full(SEMVER, version) {
        return Err("version must be canonical MAJOR.MINOR.PATCH".into());
    }
    validate_tag_prefix(prefix)?;
    let version_tag = format!("v{version}");
    Ok(ReleaseTag {
        version: version.to_owned(),
        full_tag: if prefix.is_empty() {
            version_tag.clone()
        } else {
            format!("{prefix}/{version_tag}")
        },
        artifact_tag: if prefix.is_empty() {
            version_tag.clone()
        } else {
            format!("{prefix}-{version_tag}")
        },
        version_tag,
        tag_prefix: prefix.to_owned(),
    })
}

pub fn parse_release_tag(tag: &str, prefix: &str) -> Result<ReleaseTag> {
    validate_tag_prefix(prefix)?;
    let start = if prefix.is_empty() {
        "v".to_owned()
    } else {
        format!("{prefix}/v")
    };
    let version = tag
        .strip_prefix(&start)
        .ok_or("tag does not match the supplied tag-prefix")?;
    // The derived full tag is `start` followed by `version`, which is `tag`
    // itself, so the script's `full_tag != tag` re-check cannot fail.
    derive_release_tag(version, prefix)
}

fn index_entries(root: &Path, relative: &str) -> Result<Vec<(String, Vec<u8>)>> {
    let out = run(root, "git", &["ls-files", "--stage", "-z", "--", relative])?;
    parse_index_entries(out.status.success(), &out.stdout)
}

/// The (mode, path) records `git ls-files --stage -z` printed.
pub fn parse_index_entries(success: bool, stdout: &[u8]) -> Result<Vec<(String, Vec<u8>)>> {
    if !success {
        return Err("source-directory could not be checked against the Git index".into());
    }
    let mut entries = Vec::new();
    for record in stdout.split(|b| *b == 0).filter(|r| !r.is_empty()) {
        let tab = record
            .iter()
            .position(|b| *b == b'\t')
            .ok_or("source-directory Git index data is malformed")?;
        let mode = record[..tab].split(|b| *b == b' ').next().unwrap();
        entries.push((
            std::str::from_utf8(mode)
                .map_err(|e| e.to_string())?
                .to_owned(),
            record[tab + 1..].to_vec(),
        ));
    }
    Ok(entries)
}

pub fn validate_source_directory(root: &Path, supplied: &str) -> Result<String> {
    let root = absolute(root)?;
    if supplied == "." {
        return Ok(".".into());
    }
    let invalid = "source-directory must be a relative POSIX directory";
    if supplied.is_empty()
        || supplied.contains('\\')
        || supplied.chars().any(|c| c < ' ' || c == '\x7f')
        || supplied
            .split('/')
            .any(|p| matches!(p, "" | "." | "..") || !full("[A-Za-z0-9._-]+", p))
    {
        return Err(invalid.into());
    }
    let mut current = root.clone();
    let mut relative = Vec::new();
    for part in supplied.split('/') {
        current.push(part);
        relative.push(part);
        if symlink(&current) {
            return Err(format!(
                "source-directory must not traverse a symbolic link: {supplied}"
            ));
        }
        let component = relative.join("/");
        for (mode, path) in index_entries(&root, &component)? {
            if path == component.as_bytes() && matches!(mode.as_str(), "120000" | "160000") {
                return Err(format!(
                    "source-directory must not traverse a symbolic link or gitlink: {supplied}"
                ));
            }
        }
    }
    if !current.exists() {
        return Err(format!("source-directory is missing: {supplied}"));
    }
    let resolved = absolute(&current)?;
    if resolved == root || !resolved.starts_with(&root) || !resolved.is_dir() {
        return Err("source-directory must be a directory inside the checkout".into());
    }
    let prefix = format!("{supplied}/");
    if !index_entries(&root, supplied)?
        .iter()
        .any(|(_, p)| p.starts_with(prefix.as_bytes()))
    {
        return Err(format!(
            "source-directory must contain tracked Git files: {supplied}"
        ));
    }
    Ok(supplied.into())
}

pub fn safe_relative_path(supplied: &str) -> Result<String> {
    if supplied.contains('\\') {
        return Err("version-file must use a safe relative POSIX path".into());
    }
    // PurePosixPath drops redundant separators and dot components before checking.
    let parts = supplied
        .split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect::<Vec<_>>();
    if supplied.is_empty() || supplied.starts_with('/') || parts.contains(&"..") {
        return Err("version-file must be a safe relative POSIX path".into());
    }
    // "." and "./" name the checkout itself; the regular-file check refuses them.
    Ok(if parts.is_empty() {
        ".".into()
    } else {
        parts.join("/")
    })
}

pub fn tracked_version_file(root: &Path, supplied: &str) -> Result<PathBuf> {
    let root = absolute(root)?;
    let relative = safe_relative_path(supplied)?;
    let mut current = root.clone();
    for part in relative.split('/') {
        current.push(part);
        if symlink(&current) {
            return Err(format!(
                "version-file must not traverse a symbolic link: {supplied}"
            ));
        }
    }
    if !current.exists() {
        return Err(format!("version-file is missing: {supplied}"));
    }
    let resolved = absolute(&current)?;
    if resolved == root || !resolved.starts_with(&root) || !resolved.is_file() {
        return Err("version-file must be a regular file inside the checkout".into());
    }
    if !run(
        &root,
        "git",
        &["ls-files", "--error-unmatch", "--", &relative],
    )?
    .status
    .success()
    {
        return Err(format!("version-file must be tracked by Git: {supplied}"));
    }
    if resolved
        .metadata()
        .map_err(|e| io_error(e, &resolved))?
        .len()
        > MAX_METADATA
    {
        return Err("version-file exceeds the 1 MiB metadata limit".into());
    }
    Ok(resolved)
}

pub fn derive_metadata(root: &Path, parser: &str, file: &str) -> Result<Value> {
    if !matches!(parser, "pyproject" | "python-literal") {
        return Err("version-parser must be 'pyproject' or 'python-literal'".into());
    }
    let project = tracked_version_file(root, "pyproject.toml")?;
    let name = project_name(&read_utf8(&project, "pyproject.toml")?)?;
    let path = tracked_version_file(root, file)?;
    let text = read_utf8(&path, "version-file")?;
    let version = if parser == "pyproject" {
        pyproject_version(&text)?
    } else {
        python_literal_version(&text)?
    };
    if !full(SEMVER, &version) {
        return Err("version must be canonical MAJOR.MINOR.PATCH".into());
    }
    let stem = wheel_stem(&name)?;
    Ok(json!({"name": name, "stem": stem, "version": version}))
}

/// `[project].name` of a pyproject.toml text.
pub fn project_name(text: &str) -> Result<String> {
    let missing = "pyproject.toml must declare [project].name";
    let doc = text.parse::<toml::Table>().map_err(|_| missing)?;
    let value = doc
        .get("project")
        .and_then(|v| v.get("name"))
        .ok_or(missing)?;
    value
        .as_str()
        .filter(|s| !py::strip(s).is_empty())
        .map(str::to_owned)
        .ok_or("pyproject.toml [project].name must be a non-empty string".into())
}

/// `[project].version` of a pyproject.toml text, for the pyproject parser.
pub fn pyproject_version(text: &str) -> Result<String> {
    let missing = "pyproject parser requires [project].version";
    let doc = text.parse::<toml::Table>().map_err(|_| missing)?;
    doc.get("project")
        .and_then(|v| v.get("version"))
        .ok_or(missing)?
        .as_str()
        .map(str::to_owned)
        .ok_or("pyproject [project].version must be a string".into())
}

/// PEP 503 / binary-distribution filename normalisation: every modern build
/// backend emits this exact stem, so the release must look for that name.
pub fn wheel_stem(name: &str) -> Result<String> {
    let stem = regex::Regex::new("[-_.]+")
        .unwrap()
        .replace_all(name, "_")
        .to_lowercase();
    if !full(STEM, &stem) {
        return Err("project name does not produce a safe wheel stem".into());
    }
    Ok(stem)
}

/// What `ast.literal_eval` makes of an expression: `None` where it raises
/// ValueError or TypeError, otherwise the value's kind.
#[derive(Clone, Copy, PartialEq)]
enum Literal {
    Str,
    Int,
    Float,
    Complex,
    /// Any other constant, tuple or frozen value: hashable.
    Hashable,
    /// A list, dict or set.
    Unhashable,
}

fn literal_eval(expr: &ruff_python_ast::Expr) -> Option<Literal> {
    use ruff_python_ast::{Expr, Number, Operator, UnaryOp};
    let number = |expr: &Expr| match expr {
        Expr::NumberLiteral(n) => Some(match n.value {
            Number::Int(_) => Literal::Int,
            Number::Float(_) => Literal::Float,
            Number::Complex { .. } => Literal::Complex,
        }),
        _ => None,
    };
    let signed = |expr: &Expr| match expr {
        Expr::UnaryOp(u) if matches!(u.op, UnaryOp::UAdd | UnaryOp::USub) => number(&u.operand),
        other => number(other),
    };
    let hashable = |items: &[Literal]| items.iter().all(|i| *i != Literal::Unhashable);
    match expr {
        Expr::StringLiteral(_) => Some(Literal::Str),
        Expr::BytesLiteral(_)
        | Expr::BooleanLiteral(_)
        | Expr::NoneLiteral(_)
        | Expr::EllipsisLiteral(_) => Some(Literal::Hashable),
        Expr::Tuple(t) => {
            let items = t
                .elts
                .iter()
                .map(literal_eval)
                .collect::<Option<Vec<_>>>()?;
            Some(if hashable(&items) {
                Literal::Hashable
            } else {
                Literal::Unhashable
            })
        }
        Expr::List(l) => {
            l.elts
                .iter()
                .map(literal_eval)
                .collect::<Option<Vec<_>>>()?;
            Some(Literal::Unhashable)
        }
        Expr::Set(s) => {
            let items = s
                .elts
                .iter()
                .map(literal_eval)
                .collect::<Option<Vec<_>>>()?;
            hashable(&items).then_some(Literal::Unhashable)
        }
        Expr::Call(c)
            if matches!(&*c.func, Expr::Name(n) if n.id.as_str() == "set")
                && c.arguments.args.is_empty()
                && c.arguments.keywords.is_empty() =>
        {
            Some(Literal::Unhashable)
        }
        Expr::Dict(d) => {
            // Keys are converted before values, then the dict is built.
            let keys = d
                .items
                .iter()
                .map(|item| item.key.as_ref().and_then(literal_eval))
                .collect::<Option<Vec<_>>>()?;
            d.items
                .iter()
                .map(|item| literal_eval(&item.value))
                .collect::<Option<Vec<_>>>()?;
            hashable(&keys).then_some(Literal::Unhashable)
        }
        Expr::BinOp(b)
            if matches!(b.op, Operator::Add | Operator::Sub)
                && matches!(signed(&b.left), Some(Literal::Int | Literal::Float))
                && number(&b.right) == Some(Literal::Complex) =>
        {
            Some(Literal::Complex)
        }
        other => signed(other),
    }
}

/// CPython refuses a decimal integer literal of more than 4300 digits while parsing.
fn has_overlong_int(text: &str, module: &ruff_python_ast::ModModule) -> bool {
    use ruff_python_ast::visitor::{Visitor, walk_expr};
    use ruff_text_size::Ranged;
    struct Scan<'a>(&'a str, bool);
    impl<'a> Visitor<'a> for Scan<'_> {
        fn visit_expr(&mut self, expr: &'a ruff_python_ast::Expr) {
            if let ruff_python_ast::Expr::NumberLiteral(n) = expr
                && matches!(n.value, ruff_python_ast::Number::Int(_))
            {
                let literal = &self.0[n.range()];
                let based = literal.len() > 1
                    && literal.as_bytes()[0] == b'0'
                    && matches!(
                        literal.as_bytes()[1],
                        b'x' | b'X' | b'o' | b'O' | b'b' | b'B'
                    );
                let digits = literal.bytes().filter(u8::is_ascii_digit).count();
                self.1 |= !based && digits > py::INT_MAX_STR_DIGITS;
            }
            walk_expr(self, expr);
        }
    }
    let mut scan = Scan(text, false);
    scan.visit_body(&module.body);
    scan.1
}

pub fn python_literal_version(text: &str) -> Result<String> {
    use ruff_python_ast::{Expr, Stmt};
    let invalid = "version-file must be valid Python source";
    let parsed = ruff_python_parser::parse_module(text).map_err(|_| invalid)?;
    let module = parsed.syntax();
    if has_overlong_int(text, module) {
        return Err(invalid.into());
    }
    let mut values = Vec::new();
    let failure = "__version__ must be one direct literal string assignment";
    for node in &module.body {
        let (targets, value): (Vec<&Expr>, Option<&Expr>) = match node {
            Stmt::Assign(a) => (a.targets.iter().collect(), Some(&a.value)),
            Stmt::AnnAssign(a) => (vec![&a.target], a.value.as_deref()),
            _ => continue,
        };
        if !targets
            .iter()
            .any(|t| matches!(t, Expr::Name(n) if n.id.as_str() == "__version__"))
        {
            continue;
        }
        if targets.len() != 1 || value.is_none() {
            return Err(failure.into());
        }
        let value = value.unwrap();
        // Keep non-string literals until the assignment count is checked.
        // literal_eval reports Literal::Str only for a string literal.
        values.push(match (literal_eval(value).ok_or(failure)?, value) {
            (Literal::Str, Expr::StringLiteral(s)) => Some(s.value.to_str().to_owned()),
            _ => None,
        });
    }
    if values.len() != 1 {
        return Err("version-file must contain exactly one __version__ assignment".into());
    }
    values.pop().unwrap().ok_or(failure.into())
}

#[derive(Clone, Debug, PartialEq)]
pub struct CandidateContext {
    pub stem: String,
    pub version: String,
    pub tag: String,
    pub repository: String,
    pub commit: String,
    pub policy_sha: String,
    pub run_id: BigInt,
    pub run_attempt: BigInt,
}

impl CandidateContext {
    pub fn validate(&self) -> Result<()> {
        if !full(STEM, &self.stem) {
            return Err("candidate stem is not safe".into());
        }
        let prefix = self.tag.rsplit_once('/').map_or("", |(p, _)| p);
        let tag = derive_release_tag(&self.version, prefix)
            .map_err(|_| "candidate tag and canonical version do not match")?;
        if tag.full_tag != self.tag {
            return Err("candidate tag and canonical version do not match".into());
        }
        if !full(REPOSITORY, &self.repository) {
            return Err("candidate repository must be owner/name".into());
        }
        if !full(SHA, &self.commit) {
            return Err("candidate source commit must be a full lower-case SHA".into());
        }
        if !full(SHA, &self.policy_sha) {
            return Err("candidate policy SHA must be a full lower-case SHA".into());
        }
        if self.run_id < BigInt::from(1) || self.run_attempt < BigInt::from(1) {
            return Err("candidate run id and attempt must be positive".into());
        }
        Ok(())
    }

    fn contract(&self) -> Vec<(String, &'static str)> {
        let base = format!("{}-{}", self.stem, self.version);
        vec![
            (format!("{base}-py3-none-any.whl"), "application/zip"),
            (format!("{base}.tar.gz"), "application/gzip"),
            (format!("{base}.spdx.json"), "application/spdx+json"),
        ]
    }
}

/// One payload file as the manifest lists it.
#[derive(Clone, Debug, PartialEq)]
pub struct CandidateAsset {
    pub name: String,
    pub media_type: String,
    pub sha256: String,
    pub size: BigInt,
}

#[derive(Clone, Debug, PartialEq)]
pub struct CandidateInventory {
    pub context: CandidateContext,
    pub assets: Vec<CandidateAsset>,
}

fn directory_files(directory: &Path) -> Result<BTreeSet<String>> {
    if symlink(directory) || !directory.is_dir() {
        return Err("candidate directory must be a regular directory".into());
    }
    let mut names = BTreeSet::new();
    for entry in std::fs::read_dir(directory).map_err(|e| io_error(e, directory))? {
        let path = entry.map_err(|e| io_error(e, directory))?.path();
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        if symlink(&path) || !path.is_file() {
            return Err(format!("candidate asset must be a regular file: {name}"));
        }
        names.insert(name);
    }
    Ok(names)
}

/// Check one payload file and return its size.
pub fn regular_asset(path: &Path) -> Result<u64> {
    let name = path.file_name().unwrap().to_string_lossy();
    if symlink(path) || !path.is_file() {
        return Err(format!("candidate asset must be a regular file: {name}"));
    }
    let size = path.metadata().map_err(|e| io_error(e, path))?.len();
    if !(1..=MAX_ASSET).contains(&size) {
        return Err(format!("candidate asset has an invalid size: {name}"));
    }
    Ok(size)
}

fn manifest(inventory: &CandidateInventory) -> Json {
    let ctx = &inventory.context;
    let text = |s: &str| Json::Str(s.to_owned());
    let object = |pairs: Vec<(&str, Json)>| {
        Json::Object(pairs.into_iter().map(|(k, v)| (k.to_owned(), v)).collect())
    };
    let assets = inventory
        .assets
        .iter()
        .map(|a| {
            object(vec![
                ("media_type", text(&a.media_type)),
                ("name", text(&a.name)),
                ("sha256", text(&a.sha256)),
                ("size", Json::Int(a.size.clone())),
            ])
        })
        .collect();
    object(vec![
        ("assets", Json::Array(assets)),
        (
            "package",
            object(vec![
                ("stem", text(&ctx.stem)),
                ("version", text(&ctx.version)),
            ]),
        ),
        (
            "policy",
            object(vec![
                ("repository", text(POLICY_REPOSITORY)),
                ("sha", text(&ctx.policy_sha)),
                ("workflow", text(POLICY_WORKFLOW)),
            ]),
        ),
        (
            "run",
            object(vec![
                ("attempt", Json::Int(ctx.run_attempt.clone())),
                ("id", Json::Int(ctx.run_id.clone())),
            ]),
        ),
        ("schema", Json::Int(1.into())),
        (
            "source",
            object(vec![
                ("commit", text(&ctx.commit)),
                ("ref", text(&format!("refs/tags/{}", ctx.tag))),
                ("repository", text(&ctx.repository)),
            ]),
        ),
    ])
}

pub fn manifest_bytes(inventory: &CandidateInventory) -> Vec<u8> {
    let options = Dump {
        indent: Some(2),
        sort_keys: true,
        ensure_ascii: true,
    };
    format!("{}\n", pyjson::dumps(&manifest(inventory), options)).into_bytes()
}

fn checksums(directory: &Path, names: &BTreeSet<String>) -> Result<String> {
    let mut text = String::new();
    for name in names {
        text.push_str(&format!(
            "{}  {name}\n",
            sha256(&read_bytes(&directory.join(name))?)
        ));
    }
    Ok(text)
}

/// Write a manifest and checksum file around exactly 3 prebuilt payloads.
pub fn write_candidate_inventory(
    directory: &Path,
    ctx: &CandidateContext,
) -> Result<CandidateInventory> {
    ctx.validate()?;
    let actual = directory_files(directory)?;
    let contract = ctx.contract();
    let mut expected = contract
        .iter()
        .map(|(n, _)| n.clone())
        .collect::<BTreeSet<_>>();
    for control in [MANIFEST, CHECKSUMS] {
        if actual.contains(control) {
            return Err(format!(
                "refusing to overwrite existing control file: {control}"
            ));
        }
    }
    if actual != expected {
        return Err(format!(
            "candidate payload inventory mismatch: expected {}, found {}",
            repr_list(expected.iter().map(String::as_str)),
            repr_list(actual.iter().map(String::as_str))
        ));
    }
    let mut assets = Vec::new();
    for (name, media_type) in &contract {
        let path = directory.join(name);
        regular_asset(&path)?;
        assets.push(CandidateAsset {
            name: name.clone(),
            media_type: (*media_type).into(),
            sha256: sha256(&read_bytes(&path)?),
            size: path
                .metadata()
                .map_err(|e| io_error(e, &path))?
                .len()
                .into(),
        });
    }
    let inventory = CandidateInventory {
        context: ctx.clone(),
        assets,
    };
    write_bytes(&directory.join(MANIFEST), &manifest_bytes(&inventory))?;
    expected.insert(MANIFEST.into());
    write_bytes(
        &directory.join(CHECKSUMS),
        checksums(directory, &expected)?.as_bytes(),
    )?;
    Ok(inventory)
}

fn mapping<'a>(value: &'a Json, label: &str, keys: &[&str]) -> Result<&'a Json> {
    let exact = match value {
        Json::Object(pairs) => {
            pairs.len() == keys.len() && keys.iter().all(|k| value.get(k).is_some())
        }
        _ => false,
    };
    if !exact {
        return Err(format!("candidate manifest {label} has an invalid shape"));
    }
    Ok(value)
}

fn field<'a>(value: &'a Json, key: &str) -> &'a Json {
    value.get(key).expect("a key the shape check required")
}

/// `int(value)` for a decoded scalar: Ok(None) where Python raises ValueError
/// or TypeError.
fn python_int(value: &Json) -> Result<Option<BigInt>> {
    use num_traits::FromPrimitive;
    Ok(match value {
        Json::Bool(b) => Some(u8::from(*b).into()),
        Json::Int(i) => Some(i.clone()),
        Json::Str(s) => py::parse_int(s, 10).ok(),
        Json::Float(f) if f.is_nan() => None,
        Json::Float(f) if f.is_infinite() => {
            return Err(
                "unexpected error: OverflowError: cannot convert float infinity to integer".into(),
            );
        }
        Json::Float(f) => BigInt::from_f64(f.trunc()),
        _ => None,
    })
}

pub fn inventory_from_manifest(document: &Json) -> Result<CandidateInventory> {
    let root = mapping(
        document,
        "root",
        &["assets", "package", "policy", "run", "schema", "source"],
    )?;
    if !field(root, "schema").py_eq(&Json::Int(1.into())) {
        return Err("candidate manifest schema is unsupported".into());
    }
    let package = mapping(field(root, "package"), "package", &["stem", "version"])?;
    let policy = mapping(
        field(root, "policy"),
        "policy",
        &["repository", "sha", "workflow"],
    )?;
    let run = mapping(field(root, "run"), "run", &["attempt", "id"])?;
    let source = mapping(
        field(root, "source"),
        "source",
        &["commit", "ref", "repository"],
    )?;
    if !field(policy, "repository").py_eq(&Json::str_obj(POLICY_REPOSITORY))
        || !field(policy, "workflow").py_eq(&Json::str_obj(POLICY_WORKFLOW))
    {
        return Err("candidate manifest policy signer is not release-python.yml".into());
    }
    let tag = field(source, "ref")
        .as_str()
        .and_then(|s| s.strip_prefix("refs/tags/"))
        .ok_or("candidate manifest source ref is invalid")?;
    let Json::Array(raw_assets) = field(root, "assets") else {
        return Err("candidate manifest assets must be a list".into());
    };
    let mut assets = Vec::new();
    for raw in raw_assets {
        let asset = mapping(raw, "asset", &["media_type", "name", "sha256", "size"])?;
        let text = |key: &str| field(asset, key).as_str();
        let (Some(media_type), Some(name), Some(digest)) =
            (text("media_type"), text("name"), text("sha256"))
        else {
            return Err("candidate manifest asset text fields are invalid".into());
        };
        // bool is a subclass of int, so isinstance admitted `true`, and a
        // one-byte asset then compared equal to it. Exact type only.
        let Json::Int(size) = field(asset, "size") else {
            return Err("candidate manifest asset size is invalid".into());
        };
        assets.push(CandidateAsset {
            name: name.into(),
            media_type: media_type.into(),
            sha256: digest.into(),
            size: size.clone(),
        });
    }
    let scalar = || "candidate manifest scalar fields are invalid".to_owned();
    let context = CandidateContext {
        stem: field(package, "stem").py_str(),
        version: field(package, "version").py_str(),
        tag: tag.into(),
        repository: field(source, "repository").py_str(),
        commit: field(source, "commit").py_str(),
        policy_sha: field(policy, "sha").py_str(),
        run_id: python_int(field(run, "id"))?.ok_or_else(scalar)?,
        run_attempt: python_int(field(run, "attempt"))?.ok_or_else(scalar)?,
    };
    context.validate()?;
    Ok(CandidateInventory { context, assets })
}

/// Fail closed unless candidate data and the expected workflow context agree.
pub fn verify_candidate_inventory(
    directory: &Path,
    ctx: &CandidateContext,
) -> Result<CandidateInventory> {
    let actual_files = directory_files(directory)?;
    let contract = ctx.contract();
    let mut payloads = contract
        .iter()
        .map(|(n, _)| n.clone())
        .collect::<BTreeSet<_>>();
    let mut expected_files = payloads.clone();
    expected_files.extend([MANIFEST.into(), CHECKSUMS.into()]);
    if actual_files != expected_files {
        return Err(format!(
            "candidate inventory mismatch: expected {}, found {}",
            repr_list(expected_files.iter().map(String::as_str)),
            repr_list(actual_files.iter().map(String::as_str))
        ));
    }
    let path = directory.join(MANIFEST);
    if path.metadata().map_err(|e| io_error(e, &path))?.len() > MAX_METADATA {
        return Err("candidate manifest exceeds the 1 MiB limit".into());
    }
    let text = read_utf8(&path, MANIFEST)?;
    let document = pyjson::loads(&text).map_err(|_| "candidate manifest is not valid JSON")?;
    let inventory = inventory_from_manifest(&document)?;
    if read_bytes(&path)? != manifest_bytes(&inventory) {
        return Err("candidate manifest is not canonical JSON".into());
    }
    let found = &inventory.context;
    for (a, b, label) in [
        (&found.stem, &ctx.stem, "package stem"),
        (&found.version, &ctx.version, "package version"),
        (&found.tag, &ctx.tag, "source tag"),
        (&found.repository, &ctx.repository, "source repository"),
        (&found.commit, &ctx.commit, "source commit"),
        (&found.policy_sha, &ctx.policy_sha, "policy SHA"),
    ] {
        if a != b {
            return Err(format!(
                "candidate {label} mismatch: {} != {}",
                py::repr_str(a),
                py::repr_str(b)
            ));
        }
    }
    for (a, b, label) in [
        (&found.run_id, &ctx.run_id, "run id"),
        (&found.run_attempt, &ctx.run_attempt, "run attempt"),
    ] {
        if a != b {
            return Err(format!("candidate {label} mismatch: {a} != {b}"));
        }
    }
    let names = inventory
        .assets
        .iter()
        .map(|a| a.name.clone())
        .collect::<BTreeSet<_>>();
    if names != payloads {
        return Err("candidate manifest payload inventory is invalid".into());
    }
    if inventory.assets.len() != contract.len() {
        return Err("candidate manifest contains duplicate assets".into());
    }
    for asset in &inventory.assets {
        let path = directory.join(&asset.name);
        let size = regular_asset(&path)?;
        let media = contract.iter().find(|(n, _)| *n == asset.name).unwrap().1;
        if asset.media_type != media {
            return Err(format!("candidate media type mismatch: {}", asset.name));
        }
        if asset.size != size.into() {
            return Err(format!("candidate size mismatch: {}", asset.name));
        }
        if asset.sha256 != sha256(&read_bytes(&path)?) {
            return Err(format!("candidate digest mismatch: {}", asset.name));
        }
    }
    payloads.insert(MANIFEST.into());
    if read_utf8(&directory.join(CHECKSUMS), CHECKSUMS)? != checksums(directory, &payloads)? {
        return Err("candidate checksum inventory or digest mismatch".into());
    }
    Ok(inventory)
}
