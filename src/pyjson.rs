//! JSON read and written as the original builder's `json` module did.
//!
//! Documents are read with Python's C scanner semantics, so a refusal names
//! the same position, and a repeated key keeps its first position and last
//! value as a dict does. Output matches `json.dumps` with the given options.

use num_bigint::BigInt;

use crate::py;

/// A decoded JSON value, keeping object key order.
#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(BigInt),
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    Object(Vec<(String, Json)>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn str_obj(value: &str) -> Json {
        Json::Str(value.to_string())
    }

    /// `repr(value)` of the Python object.
    pub fn repr(&self) -> String {
        match self {
            Json::Null => "None".into(),
            Json::Bool(true) => "True".into(),
            Json::Bool(false) => "False".into(),
            Json::Int(i) => py::int_repr(i).unwrap_or_else(|e| e),
            Json::Float(f) => py::float_repr(*f),
            Json::Str(s) => py::repr_str(s),
            Json::Array(items) => format!(
                "[{}]",
                items.iter().map(Json::repr).collect::<Vec<_>>().join(", ")
            ),
            Json::Object(pairs) => format!(
                "{{{}}}",
                pairs
                    .iter()
                    .map(|(k, v)| format!("{}: {}", py::repr_str(k), v.repr()))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        }
    }

    /// `str(value)` of the Python object.
    pub fn py_str(&self) -> String {
        match self {
            Json::Str(s) => s.clone(),
            other => other.repr(),
        }
    }

    /// Python `==` between two decoded values: `True == 1 == 1.0`, dicts
    /// compare without order, and NaN equals nothing.
    pub fn py_eq(&self, other: &Json) -> bool {
        fn int(value: &Json) -> Option<BigInt> {
            match value {
                Json::Bool(b) => Some(BigInt::from(u8::from(*b))),
                Json::Int(i) => Some(i.clone()),
                _ => None,
            }
        }
        fn int_float(i: &BigInt, f: f64) -> bool {
            f.is_finite() && f.fract() == 0.0 && num_bigint::BigInt::from_f64(f).as_ref() == Some(i)
        }
        use num_traits::FromPrimitive;
        match (self, other) {
            (Json::Null, Json::Null) => true,
            (Json::Float(a), Json::Float(b)) => a == b,
            (Json::Float(f), x) | (x, Json::Float(f)) => int(x).is_some_and(|i| int_float(&i, *f)),
            (Json::Str(a), Json::Str(b)) => a == b,
            (Json::Array(a), Json::Array(b)) => {
                a.len() == b.len() && a.iter().zip(b).all(|(x, y)| x.py_eq(y))
            }
            (Json::Object(a), Json::Object(b)) => {
                a.len() == b.len()
                    && a.iter()
                        .all(|(k, v)| other.get(k).is_some_and(|w| v.py_eq(w)))
            }
            (a, b) => match (int(a), int(b)) {
                (Some(x), Some(y)) => x == y,
                _ => false,
            },
        }
    }

    /// Python truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            Json::Null => false,
            Json::Bool(b) => *b,
            Json::Int(i) => i != &BigInt::from(0),
            Json::Float(f) => *f != 0.0,
            Json::Str(s) => !s.is_empty(),
            Json::Array(items) => !items.is_empty(),
            Json::Object(pairs) => !pairs.is_empty(),
        }
    }
}

/// Why a JSON text could not be read, rendered as Python's exception.
#[derive(Debug, Clone, PartialEq)]
pub struct DecodeError(pub String);

impl std::fmt::Display for DecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

fn decode_error(msg: &str, doc: &[char], pos: usize) -> DecodeError {
    let lineno = doc[..pos].iter().filter(|c| **c == '\n').count() + 1;
    let colno = match doc[..pos].iter().rposition(|c| *c == '\n') {
        Some(i) => pos - i,
        None => pos + 1,
    };
    DecodeError(format!("{msg}: line {lineno} column {colno} (char {pos})"))
}

fn is_ws(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r')
}

enum Frame {
    Array(Vec<Json>),
    Object(Vec<(String, Json)>, String),
}

/// `json.loads(raw)` for bytes: the encoding comes from `json.detect_encoding`.
pub fn loads_bytes(raw: &[u8]) -> Result<Json, DecodeError> {
    let unicode = |units: Vec<u32>| -> Result<String, DecodeError> {
        // shortcut: Python keeps lone surrogates ('surrogatepass'); they are refused.
        units
            .into_iter()
            .map(|u| char::from_u32(u).ok_or_else(|| DecodeError("invalid code point".into())))
            .collect()
    };
    let utf16 = |raw: &[u8], big: bool| -> Result<String, DecodeError> {
        if !raw.len().is_multiple_of(2) {
            return Err(DecodeError("truncated data".into()));
        }
        let units = raw.chunks(2).map(|c| {
            if big {
                u16::from_be_bytes([c[0], c[1]])
            } else {
                u16::from_le_bytes([c[0], c[1]])
            }
        });
        char::decode_utf16(units)
            .collect::<Result<String, _>>()
            .map_err(|e| DecodeError(e.to_string()))
    };
    let utf32 = |raw: &[u8], big: bool| -> Result<String, DecodeError> {
        if !raw.len().is_multiple_of(4) {
            return Err(DecodeError("truncated data".into()));
        }
        unicode(
            raw.chunks(4)
                .map(|c| {
                    let c = [c[0], c[1], c[2], c[3]];
                    if big {
                        u32::from_be_bytes(c)
                    } else {
                        u32::from_le_bytes(c)
                    }
                })
                .collect(),
        )
    };
    let text = if let Some(rest) = raw.strip_prefix(b"\x00\x00\xfe\xff") {
        utf32(rest, true)?
    } else if let Some(rest) = raw.strip_prefix(b"\xff\xfe\x00\x00") {
        utf32(rest, false)?
    } else if let Some(rest) = raw.strip_prefix(b"\xfe\xff") {
        utf16(rest, true)?
    } else if let Some(rest) = raw.strip_prefix(b"\xff\xfe") {
        utf16(rest, false)?
    } else if let Some(rest) = raw.strip_prefix(b"\xef\xbb\xbf") {
        py::decode_utf8(rest).map_err(|e| DecodeError(e.to_string()))?
    } else if raw.len() >= 4 && raw[0] == 0 {
        if raw[1] != 0 {
            utf16(raw, true)?
        } else {
            utf32(raw, true)?
        }
    } else if raw.len() >= 4 && raw[1] == 0 {
        if raw[2] != 0 || raw[3] != 0 {
            utf16(raw, false)?
        } else {
            utf32(raw, false)?
        }
    } else if raw.len() == 2 && raw[0] == 0 {
        utf16(raw, true)?
    } else if raw.len() == 2 && raw[1] == 0 {
        utf16(raw, false)?
    } else {
        py::decode_utf8(raw).map_err(|e| DecodeError(e.to_string()))?
    };
    loads(&text)
}

/// `json.loads(text)`.
pub fn loads(text: &str) -> Result<Json, DecodeError> {
    let s: Vec<char> = text.chars().collect();
    if s.first() == Some(&'\u{feff}') {
        return Err(decode_error(
            "Unexpected UTF-8 BOM (decode using utf-8-sig)",
            &s,
            0,
        ));
    }
    let len = s.len();
    let skip = |mut i: usize| {
        while i < len && is_ws(s[i]) {
            i += 1;
        }
        i
    };
    let mut idx = skip(0);
    let mut stack: Vec<Frame> = Vec::new();
    let expecting_value = |pos: usize| decode_error("Expecting value", &s, pos);

    // Reads a key and its delimiter, leaving idx at the value.
    let read_key = |idx: &mut usize| -> Result<String, DecodeError> {
        if *idx >= len || s[*idx] != '"' {
            return Err(decode_error(
                "Expecting property name enclosed in double quotes",
                &s,
                *idx,
            ));
        }
        let (key, next) = scanstring(&s, *idx + 1)?;
        *idx = skip(next);
        if *idx >= len || s[*idx] != ':' {
            return Err(decode_error("Expecting ':' delimiter", &s, *idx));
        }
        *idx = skip(*idx + 1);
        Ok(key)
    };

    'value: loop {
        // Scan one value starting at idx, or open a container.
        if idx >= len {
            return Err(expecting_value(idx));
        }
        let mut value = match s[idx] {
            '"' => {
                let (string, next) = scanstring(&s, idx + 1)?;
                idx = next;
                Json::Str(string)
            }
            '{' => {
                idx = skip(idx + 1);
                if idx < len && s[idx] == '}' {
                    idx += 1;
                    Json::Object(Vec::new())
                } else {
                    let key = read_key(&mut idx)?;
                    stack.push(Frame::Object(Vec::new(), key));
                    continue 'value;
                }
            }
            '[' => {
                idx = skip(idx + 1);
                if idx < len && s[idx] == ']' {
                    idx += 1;
                    Json::Array(Vec::new())
                } else {
                    stack.push(Frame::Array(Vec::new()));
                    continue 'value;
                }
            }
            _ => {
                let literal = |word: &str| {
                    s[idx..]
                        .iter()
                        .take(word.chars().count())
                        .copied()
                        .eq(word.chars())
                };
                if literal("null") {
                    idx += 4;
                    Json::Null
                } else if literal("true") {
                    idx += 4;
                    Json::Bool(true)
                } else if literal("false") {
                    idx += 5;
                    Json::Bool(false)
                } else if literal("NaN") {
                    idx += 3;
                    Json::Float(f64::NAN)
                } else if literal("Infinity") {
                    idx += 8;
                    Json::Float(f64::INFINITY)
                } else if literal("-Infinity") {
                    idx += 9;
                    Json::Float(f64::NEG_INFINITY)
                } else {
                    let (number, next) =
                        match_number(&s, idx).ok_or_else(|| expecting_value(idx))??;
                    idx = next;
                    number
                }
            }
        };
        // Attach the finished value to its container, closing any that end here.
        loop {
            let Some(frame) = stack.last_mut() else {
                let end = skip(idx);
                if end != len {
                    return Err(decode_error("Extra data", &s, end));
                }
                return Ok(value);
            };
            match frame {
                Frame::Array(items) => {
                    items.push(value);
                    idx = skip(idx);
                    if idx < len && s[idx] == ']' {
                        idx += 1;
                        value = Json::Array(std::mem::take(items));
                        stack.pop();
                        continue;
                    }
                    if idx >= len || s[idx] != ',' {
                        return Err(decode_error("Expecting ',' delimiter", &s, idx));
                    }
                    let comma = idx;
                    idx = skip(idx + 1);
                    if idx < len && s[idx] == ']' {
                        return Err(decode_error(
                            "Illegal trailing comma before end of array",
                            &s,
                            comma,
                        ));
                    }
                    continue 'value;
                }
                Frame::Object(pairs, key) => {
                    pairs.push((std::mem::take(key), value));
                    idx = skip(idx);
                    if idx < len && s[idx] == '}' {
                        idx += 1;
                        value = Json::Object(dict(std::mem::take(pairs)));
                        stack.pop();
                        continue;
                    }
                    if idx >= len || s[idx] != ',' {
                        return Err(decode_error("Expecting ',' delimiter", &s, idx));
                    }
                    let comma = idx;
                    idx = skip(idx + 1);
                    if idx < len && s[idx] == '}' {
                        return Err(decode_error(
                            "Illegal trailing comma before end of object",
                            &s,
                            comma,
                        ));
                    }
                    let next_key = read_key(&mut idx)?;
                    if let Some(Frame::Object(_, key)) = stack.last_mut() {
                        *key = next_key;
                    }
                    continue 'value;
                }
            }
        }
    }
}

/// The C scanner's number match; None where it raises StopIteration.
fn match_number(s: &[char], start: usize) -> Option<Result<(Json, usize), DecodeError>> {
    let len = s.len();
    let digit = |i: usize| i < len && s[i].is_ascii_digit();
    let mut idx = start;
    if s[idx] == '-' {
        idx += 1;
        if idx >= len {
            return None;
        }
    }
    if ('1'..='9').contains(&s[idx]) {
        idx += 1;
        while digit(idx) {
            idx += 1;
        }
    } else if s[idx] == '0' {
        idx += 1;
    } else {
        return None;
    }
    let mut is_float = false;
    if idx + 1 < len && s[idx] == '.' && s[idx + 1].is_ascii_digit() {
        is_float = true;
        idx += 2;
        while digit(idx) {
            idx += 1;
        }
    }
    if idx + 1 < len && (s[idx] == 'e' || s[idx] == 'E') {
        let e_start = idx;
        idx += 1;
        if idx + 1 < len && (s[idx] == '-' || s[idx] == '+') {
            idx += 1;
        }
        while digit(idx) {
            idx += 1;
        }
        if s[idx - 1].is_ascii_digit() {
            is_float = true;
        } else {
            idx = e_start;
        }
    }
    let text: String = s[start..idx].iter().collect();
    if is_float {
        let value: f64 = text.parse().expect("a JSON float literal");
        Some(Ok((Json::Float(value), idx)))
    } else {
        Some(
            py::parse_int(&text, 10)
                .map(|i| (Json::Int(i), idx))
                .map_err(DecodeError),
        )
    }
}

/// The C `scanstring`: `end` is the index after the opening quote.
fn scanstring(s: &[char], end: usize) -> Result<(String, usize), DecodeError> {
    let len = s.len();
    let begin = end - 1;
    let mut out = String::new();
    let mut end = end;
    loop {
        let mut next = end;
        let mut c = '\0';
        while next < len {
            c = s[next];
            if c == '"' || c == '\\' {
                break;
            }
            if (c as u32) <= 0x1f {
                return Err(decode_error("Invalid control character at", s, next));
            }
            next += 1;
        }
        if next >= len || c != '"' && c != '\\' {
            return Err(decode_error("Unterminated string starting at", s, begin));
        }
        out.extend(&s[end..next]);
        next += 1;
        if c == '"' {
            return Ok((out, next));
        }
        if next == len {
            return Err(decode_error("Unterminated string starting at", s, begin));
        }
        let c = s[next];
        if c != 'u' {
            end = next + 1;
            let replacement = match c {
                '"' => '"',
                '\\' => '\\',
                '/' => '/',
                'b' => '\x08',
                'f' => '\x0c',
                'n' => '\n',
                'r' => '\r',
                't' => '\t',
                _ => return Err(decode_error("Invalid \\escape", s, end - 2)),
            };
            out.push(replacement);
            continue;
        }
        next += 1;
        end = next + 4;
        if end >= len {
            return Err(decode_error("Invalid \\uXXXX escape", s, next - 1));
        }
        let hex = |from: usize, end: usize| -> Result<u32, DecodeError> {
            let mut value = 0;
            for &digit in &s[from..from + 4] {
                value = value << 4
                    | digit
                        .to_digit(16)
                        .ok_or_else(|| decode_error("Invalid \\uXXXX escape", s, end - 5))?;
            }
            Ok(value)
        };
        let mut code = hex(next, end)?;
        if (0xd800..=0xdbff).contains(&code) && end + 6 < len && s[end] == '\\' && s[end + 1] == 'u'
        {
            let low = hex(end + 2, end + 6)?;
            if (0xdc00..=0xdfff).contains(&low) {
                code = 0x10000 + (((code - 0xd800) << 10) | (low - 0xdc00));
                end += 6;
            }
        }
        match char::from_u32(code) {
            Some(ch) => out.push(ch),
            // shortcut: Python keeps a lone surrogate in the string, and the
            // builder could never write it out; it is refused at the escape.
            None => {
                return Err(decode_error(
                    "Unpaired surrogate in \\uXXXX escape",
                    s,
                    next - 2,
                ));
            }
        }
    }
}

/// A repeated key keeps its first position and its last value, as a dict does.
fn dict(pairs: Vec<(String, Json)>) -> Vec<(String, Json)> {
    let mut index = std::collections::HashMap::with_capacity(pairs.len());
    let mut out: Vec<(String, Json)> = Vec::with_capacity(pairs.len());
    for (key, value) in pairs {
        match index.get(&key) {
            Some(&slot) => out[slot] = (key, value),
            None => {
                index.insert(key.clone(), out.len());
                out.push((key, value));
            }
        }
    }
    out
}

/// The `json.dumps` options this tooling uses.
#[derive(Clone, Copy, Debug)]
pub struct Dump {
    pub indent: Option<usize>,
    pub sort_keys: bool,
    pub ensure_ascii: bool,
}

impl Default for Dump {
    /// `json.dumps(value)`.
    fn default() -> Self {
        Dump {
            indent: None,
            sort_keys: false,
            ensure_ascii: true,
        }
    }
}

fn dump_str(s: &str, ascii: bool, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x08' => out.push_str("\\b"),
            '\x0c' => out.push_str("\\f"),
            _ if (c as u32) < 0x20 || (ascii && !c.is_ascii()) => {
                for unit in c.encode_utf16(&mut [0; 2]) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            _ => out.push(c),
        }
    }
    out.push('"');
}

fn dump_into(value: &Json, level: usize, options: Dump, out: &mut String) {
    let newline = |out: &mut String, n: usize| {
        if let Some(width) = options.indent {
            out.push('\n');
            out.push_str(&" ".repeat(width * n));
        }
    };
    let item_separator = if options.indent.is_some() { "," } else { ", " };
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(i) => out.push_str(&i.to_string()),
        Json::Float(f) if f.is_nan() => out.push_str("NaN"),
        Json::Float(f) if f.is_infinite() => {
            out.push_str(if *f > 0.0 { "Infinity" } else { "-Infinity" })
        }
        Json::Float(f) => out.push_str(&py::float_repr(*f)),
        Json::Str(s) => dump_str(s, options.ensure_ascii, out),
        Json::Array(items) if items.is_empty() => out.push_str("[]"),
        Json::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_separator);
                }
                newline(out, level + 1);
                dump_into(item, level + 1, options, out);
            }
            newline(out, level);
            out.push(']');
        }
        Json::Object(pairs) if pairs.is_empty() => out.push_str("{}"),
        Json::Object(pairs) => {
            let mut order: Vec<&(String, Json)> = pairs.iter().collect();
            if options.sort_keys {
                order.sort_by(|a, b| a.0.cmp(&b.0));
            }
            out.push('{');
            for (i, (k, v)) in order.into_iter().enumerate() {
                if i > 0 {
                    out.push_str(item_separator);
                }
                newline(out, level + 1);
                dump_str(k, options.ensure_ascii, out);
                out.push_str(": ");
                dump_into(v, level + 1, options, out);
            }
            newline(out, level);
            out.push('}');
        }
    }
}

/// `json.dumps(value, ...)` with the given options.
pub fn dumps(value: &Json, options: Dump) -> String {
    let mut out = String::new();
    dump_into(value, 0, options, &mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn err(text: &str) -> String {
        loads(text).unwrap_err().0
    }

    #[test]
    fn errors_match_the_c_scanner() {
        assert_eq!(
            err("{\"a\":1,}"),
            "Illegal trailing comma before end of object: line 1 column 7 (char 6)"
        );
        assert_eq!(
            err("[1,]"),
            "Illegal trailing comma before end of array: line 1 column 3 (char 2)"
        );
        assert_eq!(
            err("\"\u{1}\""),
            "Invalid control character at: line 1 column 2 (char 1)"
        );
        assert_eq!(err("\"\\q\""), "Invalid \\escape: line 1 column 2 (char 1)");
        assert_eq!(
            err("\"\\u12\""),
            "Invalid \\uXXXX escape: line 1 column 3 (char 2)"
        );
        assert_eq!(
            err("\"abc"),
            "Unterminated string starting at: line 1 column 1 (char 0)"
        );
        assert_eq!(
            err("{\"a\" 1}"),
            "Expecting ':' delimiter: line 1 column 6 (char 5)"
        );
        assert_eq!(err("1 2"), "Extra data: line 1 column 3 (char 2)");
        assert_eq!(err(""), "Expecting value: line 1 column 1 (char 0)");
        assert_eq!(
            err("\u{feff}{}"),
            "Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)"
        );
        assert_eq!(
            err("[1 2]"),
            "Expecting ',' delimiter: line 1 column 4 (char 3)"
        );
        assert_eq!(
            err("{1:2}"),
            "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
        );
        assert_eq!(err("-"), "Expecting value: line 1 column 1 (char 0)");
        assert_eq!(err("01"), "Extra data: line 1 column 2 (char 1)");
        assert_eq!(
            err("\"\\"),
            "Unterminated string starting at: line 1 column 1 (char 0)"
        );
        assert_eq!(err("{\"a\":"), "Expecting value: line 1 column 6 (char 5)");
        assert_eq!(
            err("{\"a\":1"),
            "Expecting ',' delimiter: line 1 column 7 (char 6)"
        );
        assert_eq!(err("["), "Expecting value: line 1 column 2 (char 1)");
        assert_eq!(
            loads("{\"a\":1,\"b\":2,\"a\":3}").unwrap(),
            Json::Object(vec![
                ("a".into(), Json::Int(3.into())),
                ("b".into(), Json::Int(2.into()))
            ])
        );
    }

    #[test]
    fn dumps_matches_python() {
        let value = loads("{\"b\": [1, 2.5, \"\u{e9}\\n\\u0001\"], \"a\": {}, \"c\": []}").unwrap();
        let indented = Dump {
            indent: Some(2),
            ensure_ascii: false,
            ..Dump::default()
        };
        assert_eq!(
            dumps(&value, indented),
            "{\n  \"b\": [\n    1,\n    2.5,\n    \"\u{e9}\\n\\u0001\"\n  ],\n  \"a\": {},\n  \"c\": []\n}"
        );
        let sorted = Dump {
            sort_keys: true,
            ..indented
        };
        assert!(dumps(&value, sorted).starts_with("{\n  \"a\": {}"));
        assert_eq!(
            dumps(&value, Dump::default()),
            "{\"b\": [1, 2.5, \"\\u00e9\\n\\u0001\"], \"a\": {}, \"c\": []}"
        );
        assert_eq!(
            dumps(&Json::Str("\u{1f600}".into()), Dump::default()),
            "\"\\ud83d\\ude00\""
        );
    }

    #[test]
    fn repr_matches_python_for_every_value_shape() {
        assert_eq!(Json::Null.repr(), "None");
        assert_eq!(Json::Bool(true).repr(), "True");
        assert_eq!(Json::Bool(false).repr(), "False");
        assert_eq!(Json::Int(42.into()).repr(), "42");
        assert_eq!(Json::Float(1.5).repr(), "1.5");
        assert_eq!(Json::Str("x".into()).repr(), "'x'");
        assert_eq!(
            Json::Array(vec![Json::Null, Json::Bool(true)]).repr(),
            "[None, True]"
        );
        assert_eq!(
            Json::Object(vec![("a".to_string(), Json::Bool(false))]).repr(),
            "{'a': False}"
        );
    }

    #[test]
    fn py_eq_matches_python_semantics() {
        let one = Json::Int(1.into());
        assert!(Json::Null.py_eq(&Json::Null));
        assert!(Json::Float(1.0).py_eq(&Json::Float(1.0)));
        assert!(Json::Float(1.0).py_eq(&one));
        assert!(one.py_eq(&Json::Float(1.0)));
        assert!(!Json::Float(2.5).py_eq(&one));
        assert!(!Json::Float(f64::NAN).py_eq(&Json::Float(f64::NAN)));
        assert!(Json::Str("a".into()).py_eq(&Json::Str("a".into())));
        assert!(Json::Bool(true).py_eq(&one));
        assert!(!Json::Null.py_eq(&one));
    }

    #[test]
    fn truthy_matches_python() {
        assert!(!Json::Null.truthy());
        assert!(Json::Bool(true).truthy());
        assert!(!Json::Bool(false).truthy());
        assert!(!Json::Int(0.into()).truthy());
        assert!(Json::Int((-1).into()).truthy());
        assert!(!Json::Float(0.0).truthy());
        assert!(Json::Float(-0.5).truthy());
        assert!(!Json::Str(String::new()).truthy());
        assert!(Json::Str("a".into()).truthy());
        assert!(!Json::Array(vec![]).truthy());
        assert!(Json::Array(vec![Json::Null]).truthy());
        assert!(!Json::Object(vec![]).truthy());
        assert!(Json::Object(vec![("a".into(), Json::Null)]).truthy());
    }

    #[test]
    fn decode_errors_display_their_messages() {
        assert_eq!(
            loads("").unwrap_err().to_string(),
            "Expecting value: line 1 column 1 (char 0)"
        );
    }

    #[test]
    fn loads_bytes_detects_every_python_encoding() {
        let empty = Json::Object(vec![]);
        assert_eq!(
            loads_bytes(b"\x00\x00\xfe\xff\x00\x00\x00\x7b\x00\x00\x00\x7d").unwrap(),
            empty
        );
        assert_eq!(
            loads_bytes(b"\xff\xfe\x00\x00\x7b\x00\x00\x00\x7d\x00\x00\x00").unwrap(),
            empty
        );
        assert_eq!(loads_bytes(b"\xfe\xff\x00\x7b\x00\x7d").unwrap(), empty);
        assert_eq!(loads_bytes(b"\xff\xfe\x7b\x00\x7d\x00").unwrap(), empty);
        assert_eq!(loads_bytes(b"\xef\xbb\xbf{}").unwrap(), empty);
        assert_eq!(
            loads_bytes(b"\x00\x00\x00\x7b\x00\x00\x00\x7d").unwrap(),
            empty
        );
        assert_eq!(
            loads_bytes(b"\x7b\x00\x00\x00\x7d\x00\x00\x00").unwrap(),
            empty
        );
        assert_eq!(loads_bytes(b"\x00\x7b\x00\x7d").unwrap(), empty);
        assert_eq!(loads_bytes(b"\x7b\x00\x7d\x00").unwrap(), empty);
        assert_eq!(
            loads_bytes(b"\xfe\xff\x7b").unwrap_err().0,
            "truncated data"
        );
        assert_eq!(
            loads_bytes(b"\x00\x00\xfe\xff\x00").unwrap_err().0,
            "truncated data"
        );
        assert_eq!(
            loads_bytes(b"\x00\x00\xfe\xff\x00\x00\xd8\x00")
                .unwrap_err()
                .0,
            "invalid code point"
        );
        assert!(
            loads_bytes(b"\xff\xfe\x00\xd8")
                .unwrap_err()
                .0
                .contains("surrogate")
        );
        assert!(loads_bytes(b"\xff").is_err());
        assert_eq!(
            loads_bytes(b"\x00\x7b").unwrap_err().0,
            "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
        );
        assert_eq!(
            loads_bytes(b"\x7b\x00").unwrap_err().0,
            "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
        );
    }

    #[test]
    fn loads_decodes_every_escape_arm() {
        let bs = '\u{5c}';
        let input: String = [
            bs, '\u{22}', ' ', bs, bs, ' ', bs, '/', ' ', bs, 'b', ' ', bs, 'f', ' ', bs, 'n', ' ',
            bs, 'r', ' ', bs, 't',
        ]
        .into_iter()
        .collect();
        let text = format!("\u{22}{input}\u{22}");
        assert_eq!(
            loads(&text).unwrap(),
            Json::Str("\u{22} \u{5c} / \u{8} \u{c} \u{a} \u{d} \u{9}".to_string())
        );
    }

    #[test]
    fn lone_surrogate_escapes_are_refused() {
        let message = err(r#""\ud800""#);
        assert!(
            message.contains("Unpaired surrogate in \\uXXXX escape"),
            "{message}"
        );
    }

    #[test]
    fn dumps_escapes_every_special_character() {
        let bs = '\u{5c}';
        let specials: String = [
            'q', '\u{22}', bs, '\u{8}', '\u{c}', '\u{a}', '\u{d}', '\u{9}', '\u{1}', '\u{e9}',
        ]
        .into_iter()
        .collect();
        let expected = [
            "\u{22}",
            "q",
            "\u{5c}\u{22}",
            "\u{5c}\u{5c}",
            "\u{5c}b",
            "\u{5c}f",
            "\u{5c}n",
            "\u{5c}r",
            "\u{5c}t",
            "\u{5c}u0001",
            "\u{5c}u00e9",
            "\u{22}",
        ]
        .concat();
        assert_eq!(dumps(&Json::Str(specials), Dump::default()), expected);
    }

    #[test]
    fn exponent_numbers_and_their_fallbacks() {
        assert_eq!(loads("1.25").unwrap(), Json::Float(1.25));
        assert_eq!(loads("1e10").unwrap(), Json::Float(1e10));
        assert_eq!(loads("1E+2").unwrap(), Json::Float(100.0));
        assert_eq!(loads("1e-2").unwrap(), Json::Float(0.01));
        assert_eq!(loads("-1.5").unwrap(), Json::Float(-1.5));
        assert_eq!(err("1e"), "Extra data: line 1 column 2 (char 1)");
        assert_eq!(err("1e+"), "Extra data: line 1 column 2 (char 1)");
    }
}
