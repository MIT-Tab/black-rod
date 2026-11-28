use std::collections::BTreeMap;

#[derive(Clone, Default, PartialEq, Eq)]
pub(in crate::site_admin) struct Parameters(BTreeMap<String, Vec<String>>);

impl Parameters {
    pub(super) fn new() -> Self {
        Self::default()
    }

    pub(in crate::site_admin) fn get(&self, key: &str) -> Option<&String> {
        self.0.get(key)?.last()
    }

    pub(super) fn values_for(&self, key: &str) -> &[String] {
        self.0.get(key).map_or(&[], Vec::as_slice)
    }

    pub(super) fn contains_key(&self, key: &str) -> bool {
        self.0.contains_key(key)
    }

    pub(super) fn insert(&mut self, key: String, value: String) {
        self.0.insert(key, vec![value]);
    }

    pub(super) fn remove(&mut self, key: &str) {
        self.0.remove(key);
    }

    pub(super) fn retain(&mut self, mut keep: impl FnMut(&str) -> bool) {
        self.0.retain(|key, _| keep(key));
    }

    pub(super) fn len(&self) -> usize {
        self.0.len()
    }

    pub(super) fn keys(&self) -> impl Iterator<Item = &String> {
        self.0.keys()
    }

    pub(super) fn grouped(&self) -> impl Iterator<Item = (&String, &Vec<String>)> {
        self.0.iter()
    }

    pub(super) fn iter(&self) -> impl Iterator<Item = (&String, &String)> {
        self.0
            .iter()
            .flat_map(|(key, values)| values.iter().map(move |value| (key, value)))
    }

    pub(super) fn merge(&mut self, other: Self) {
        self.0.extend(other.0);
    }
}

impl FromIterator<(String, String)> for Parameters {
    fn from_iter<T: IntoIterator<Item = (String, String)>>(items: T) -> Self {
        let mut groups = BTreeMap::<String, Vec<String>>::new();
        for (key, value) in items {
            groups.entry(key).or_default().push(value);
        }
        Self(groups)
    }
}

impl<const N: usize> From<[(String, String); N]> for Parameters {
    fn from(values: [(String, String); N]) -> Self {
        values.into_iter().collect()
    }
}
