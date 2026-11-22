use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};

const WS: &str = r"\x{0009}-\x{000d}\x{001c}-\x{0020}\x{0085}\x{00a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}";
const DEC: &str = r"\x{2500}-\x{27bf}\x{e000}-\x{f8ff}⋅↴";

fn whitespace(c: char) -> bool {
    matches!(c as u32, 9..=13 | 28..=32 | 0x85 | 0xa0 | 0x1680 | 0x2000..=0x200a | 0x2028 | 0x2029 | 0x202f | 0x205f | 0x3000)
}

fn lines(text: &str) -> Vec<&str> {
    let mut result = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((position, c)) = chars.next() {
        if matches!(c as u32, 10..=13 | 28..=30 | 0x85 | 0x2028 | 0x2029) {
            result.push(&text[start..position]);
            start = position + c.len_utf8();
            if c == '\r' && chars.peek().map(|(_, next)| *next) == Some('\n') {
                let (p, next) = chars.next().unwrap();
                start = p + next.len_utf8();
            }
        }
    }
    if start < text.len() {
        result.push(&text[start..]);
    }
    result
}

fn clean(mut value: &str) -> &str {
    value = value.trim_matches(|c| ",:;<>\"'|，。、；：！？".contains(c));
    value = value.trim_end_matches('.');
    for (open, close) in [('(', ')'), ('[', ']'), ('{', '}')] {
        if !value.ends_with(close) {
            continue;
        }
        let opens = value.chars().filter(|c| *c == open).count();
        let mut closes = value.chars().filter(|c| *c == close).count();
        while closes > opens && value.ends_with(close) {
            value = &value[..value.len() - 1];
            closes -= 1;
        }
    }
    value
}

struct Patterns {
    word: Regex,
    url: Regex,
    quote: Regex,
    ansi: Regex,
    scheme: Regex,
    exclude: Regex,
}
impl Patterns {
    fn new() -> Self {
        Self {
            word: Regex::new(&format!(
                r"[^{}\[\](){{}}=$，。、；：（）「」《》！？{}]+",
                WS, DEC
            ))
            .unwrap(),
            url: Regex::new(&format!(
                r#"(?:https?://|ssh://|git://|ftp://|file:///|git@)[^{}<>"'{}]+"#,
                WS, DEC
            ))
            .unwrap(),
            quote: Regex::new(r#""([^"\r\n]+)"|'([^'\r\n]+)'"#).unwrap(),
            ansi: Regex::new(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]").unwrap(),
            scheme: Regex::new(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://|git@)").unwrap(),
            exclude: Regex::new(r"^(?:\d+/\d+|[\d.]*[kmgKMG]?/s)$").unwrap(),
        }
    }
    fn kind(&self, value: &str) -> &'static str {
        if self.scheme.is_match(value) {
            "URL"
        } else if value.contains('/') && !self.exclude.is_match(value) {
            "Path"
        } else {
            "Word"
        }
    }
}

#[derive(Clone, Copy)]
struct Candidate<'a> {
    value: &'a str,
    detail: &'static str,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Item {
    pub id: String,
    pub title: String,
    pub detail: &'static str,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Output {
    pub items: Vec<Item>,
    pub values: BTreeMap<String, String>,
    pub source_lines: usize,
    pub limited: bool,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Options {
    pub mode: String,
    pub max_lines: usize,
    pub max_items: usize,
    pub min_length: usize,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            mode: "word".into(),
            max_lines: 3000,
            max_items: 2000,
            min_length: 3,
        }
    }
}

impl Options {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.max_lines == 0 || !(1..=2000).contains(&self.max_items) || self.min_length == 0 {
            return Err("max-items must be 1..2000; max-lines and min-length must be positive");
        }
        if !["word", "line", "path", "url", "quote"].contains(&self.mode.as_str()) {
            return Err("mode must be word, line, path, url, or quote");
        }
        Ok(())
    }
}

pub struct Extractor {
    patterns: Patterns,
}

impl Default for Extractor {
    fn default() -> Self {
        Self {
            patterns: Patterns::new(),
        }
    }
}

impl Extractor {
    pub fn extract(&self, input: &str, options: &Options) -> Result<Output, &'static str> {
        options.validate()?;
        let patterns = &self.patterns;
        let mode = options.mode.as_str();
        let max_lines = options.max_lines;
        let max_items = options.max_items;
        let min_length = options.min_length;
        let text = patterns.ansi.replace_all(input, "");
        let all_lines = lines(&text);
        let selected_lines = &all_lines[all_lines.len().saturating_sub(max_lines)..];
        let mut seen = HashSet::new();
        let mut values: Vec<Candidate> = Vec::new();
        let mut limited = false;
        'outer: for line in selected_lines.iter().rev() {
            let mut candidates = Vec::new();
            match mode {
                "line" => candidates.push(Candidate {
                    value: line.trim_matches(whitespace),
                    detail: "Line",
                }),
                "url" => {
                    for m in patterns.url.find_iter(line) {
                        candidates.push(Candidate {
                            value: clean(m.as_str()),
                            detail: "URL",
                        });
                    }
                }
                "quote" => {
                    for capture in patterns.quote.captures_iter(line) {
                        candidates.push(Candidate {
                            value: capture.get(1).or(capture.get(2)).unwrap().as_str(),
                            detail: "Quote",
                        });
                    }
                }
                _ => {
                    for m in patterns.url.find_iter(line) {
                        candidates.push(Candidate {
                            value: clean(m.as_str()),
                            detail: "URL",
                        });
                    }
                    for capture in patterns.quote.captures_iter(line) {
                        let value = capture.get(1).or(capture.get(2)).unwrap().as_str();
                        if patterns.kind(value) == "Path" {
                            candidates.push(Candidate {
                                value,
                                detail: "Path",
                            });
                        }
                    }
                    for m in patterns.word.find_iter(line) {
                        let value = clean(m.as_str());
                        candidates.push(Candidate {
                            value,
                            detail: patterns.kind(value),
                        });
                    }
                }
            }
            for candidate in candidates {
                if mode == "path" && candidate.detail != "Path" {
                    continue;
                }
                if candidate.value.chars().count() < min_length || !seen.insert(candidate.value) {
                    continue;
                }
                if values.len() == max_items {
                    limited = true;
                    break 'outer;
                }
                values.push(candidate);
            }
        }
        let mut items = Vec::with_capacity(values.len());
        let mut mapping = BTreeMap::new();
        for (index, candidate) in values.iter().enumerate() {
            let id = (index + 1).to_string();
            mapping.insert(id.clone(), candidate.value.to_owned());
            items.push(Item {
                id,
                title: candidate.value.to_owned(),
                detail: candidate.detail,
            });
        }
        Ok(Output {
            items,
            values: mapping,
            source_lines: selected_lines.len(),
            limited,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::clean;

    #[test]
    fn clean_preserves_balanced_bracket_pairs() {
        assert_eq!(
            clean("https://example.com/a_(b)"),
            "https://example.com/a_(b)"
        );
        assert_eq!(
            clean("https://example.com/a[0]"),
            "https://example.com/a[0]"
        );
        assert_eq!(
            clean("https://example.com/a{b}"),
            "https://example.com/a{b}"
        );
        assert_eq!(
            clean("https://example.com/a_(b)[c]{d}"),
            "https://example.com/a_(b)[c]{d}"
        );
    }

    #[test]
    fn clean_strips_unbalanced_trailing_brackets() {
        assert_eq!(clean("https://example.com/a)"), "https://example.com/a");
        assert_eq!(clean("https://example.com/a))"), "https://example.com/a");
        assert_eq!(clean("https://example.com/a]"), "https://example.com/a");
        assert_eq!(clean("https://example.com/a]]"), "https://example.com/a");
        assert_eq!(clean("https://example.com/a}"), "https://example.com/a");
        assert_eq!(clean("https://example.com/a}}"), "https://example.com/a");
        assert_eq!(
            clean("https://example.com/a_(b))"),
            "https://example.com/a_(b)"
        );
        assert_eq!(
            clean("https://example.com/a[0]]"),
            "https://example.com/a[0]"
        );
        assert_eq!(
            clean("https://example.com/a{b}}"),
            "https://example.com/a{b}"
        );
    }

    #[test]
    fn clean_applies_bracket_pairs_in_original_order() {
        assert_eq!(
            clean("https://example.com/a[0])"),
            "https://example.com/a[0]"
        );
        assert_eq!(
            clean("https://example.com/a(b]]"),
            "https://example.com/a(b"
        );
        assert_eq!(
            clean("https://example.com/a{b])"),
            "https://example.com/a{b"
        );
        assert_eq!(
            clean("https://example.com/a{b))"),
            "https://example.com/a{b"
        );
    }
}
