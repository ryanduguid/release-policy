//! Release policy contracts, ported from the pinned Python tooling.
pub mod archives;
pub mod attribution;
pub mod check_canaries;
pub mod check_reviews;
pub mod common;
pub mod egress;
pub mod fuzz;
pub mod git_revision;
pub mod pin_families;
pub mod py;
pub mod pyjson;
mod pyunicode;
pub mod release;
pub mod required_checks;
pub mod verify_skills;

pub type Result<T> = std::result::Result<T, String>;
