pub fn sum(left: u64, right: u64) -> u64 {
    left + right
}

#[test]
fn adds_unequal_values() {
    assert_eq!(sum(3, 7), 10);
}
