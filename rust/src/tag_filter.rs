use std::collections::BTreeSet;

pub(crate) fn parse(values: impl IntoIterator<Item = impl AsRef<str>>) -> Vec<String> {
    let input = values
        .into_iter()
        .map(|v| v.as_ref().to_owned())
        .collect::<Vec<_>>()
        .join(",");
    let mut quoted = false;
    let mut current = String::new();
    let mut unquoted = Vec::new();
    let mut tags = BTreeSet::new();
    let mut comma = false;
    for ch in input.chars() {
        if ch == '"' {
            if quoted {
                if !current.trim().is_empty() {
                    tags.insert(current.trim().to_owned());
                }
                current.clear();
            } else {
                unquoted.push(std::mem::take(&mut current));
            }
            quoted = !quoted;
        } else {
            if !quoted && ch == ',' {
                comma = true;
            }
            current.push(ch);
        }
    }
    comma |= current.contains(',');
    unquoted.push(current);
    let separator = if comma { ',' } else { ' ' };
    tags.extend(
        unquoted
            .iter()
            .flat_map(|part| part.split(separator))
            .map(str::trim)
            .filter(|tag| !tag.is_empty())
            .map(str::to_owned),
    );
    tags.into_iter().collect()
}

#[cfg(test)]
mod tests {
    use super::parse;

    #[test]
    fn reproduces_taggit_commas_quotes_and_unclosed_input() {
        assert_eq!(parse(["economic policy"]), ["economic", "policy"]);
        assert_eq!(
            parse(["economic policy", "politics"]),
            ["economic policy", "politics"]
        );
        assert_eq!(
            parse(["\"economic, policy\" common common"]),
            ["common", "economic, policy"]
        );
        assert_eq!(parse(["\"economic, policy"]), ["economic", "policy"]);
    }
}
