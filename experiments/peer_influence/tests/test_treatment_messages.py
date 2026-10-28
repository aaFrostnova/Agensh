from pathlib import Path

from peer_influence.manifest import treatment_pair_problems

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
A = "First shared sentence here. Second shared sentence here. This third one ends the first text with some words."
B = "First shared sentence here. Second shared sentence here. A different third sentence closes the other text."


def test_a_matched_pair_has_no_problems():
    assert treatment_pair_problems(A, B) == []


def test_lengths_must_be_comparable():
    assert any("length" in p for p in treatment_pair_problems(A, A + " " + "word " * 40))
    assert any("length" in p for p in treatment_pair_problems(A, "First shared sentence here. Second shared sentence here."))


def test_the_diagnosis_and_opening_must_be_shared():
    other_opening = B.replace("First shared sentence here.", "Another opening altogether.")
    assert any("opening" in p for p in treatment_pair_problems(A, other_opening))


def test_neither_message_may_prime_the_victim_about_the_study():
    for word in ("experiment", "malicious", "honeypot", "reward hack"):
        for pair in ((A + f" {word}.", B), (A, B + f" {word}.")):
            assert any(word in p for p in treatment_pair_problems(*pair)), word


def test_an_empty_message_is_a_problem():
    assert treatment_pair_problems("", B) and treatment_pair_problems(A, "  \n")


def test_the_shipped_pair_satisfies_plan_9_4():
    benign = (PROMPTS / "peer_benign.txt").read_text(encoding="utf-8")
    malicious = (PROMPTS / "peer_malicious.txt").read_text(encoding="utf-8")
    assert treatment_pair_problems(benign, malicious) == []
