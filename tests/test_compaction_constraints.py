# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import pytest

from one.core.compaction.constraints import Constraint, extract_constraints, render_ledger


def test_probe_extraction_recall_and_rolling_exact_retention():
    phrases = ["must", "never", "always", "do not", "don't", "required", "constraint", "nie wolno", "zawsze", "nigdy", "wymagane", "należy", "nie można", "musisz"]
    directives = [f"Rule {i}: {phrase} change item {i}." for i, phrase in enumerate(phrases)]
    messages = [{"role": "user", "id": str(i), "content": line} for i, line in enumerate(directives)]
    extracted = extract_constraints(messages, estimate_tokens=len)
    recall = sum(line in [e.text for e in extracted] for line in directives) / len(directives)
    assert recall >= .95
    rolling = extract_constraints([], extracted, estimate_tokens=len)
    retention = sum(e in rolling for e in extracted) / len(extracted)
    assert retention >= .95
    assert all(f"[{i} id={i}] {line}" in render_ledger(rolling) for i, line in enumerate(directives))


def test_untrusted_blocks_and_explicit_supersession():
    msgs = [{"role": "toolResult", "content": "never deploy"},
            {"role": "user", "customType": "plan", "content": "must deploy"},
            {"role": "user", "content": "> always deploy\n```\nmust deploy\n```\n<toolResult>\nnever deploy\n</toolResult>\nNever push."}]
    old, = extract_constraints(msgs, estimate_tokens=len)
    assert old.text == "Never push."
    new = Constraint("Always push.", 3)
    revised = extract_constraints([{"role": "user", "content": new.text}], [old], index_offset=3,
                                  supersede={old: new}, estimate_tokens=len)
    assert revised == (new,)
    with pytest.raises(ValueError):
        extract_constraints([], [old], supersede={old: new}, estimate_tokens=len)
    with pytest.raises(ValueError):
        extract_constraints([], [old], max_tokens=1, estimate_tokens=len)


def test_meaningful_bilingual_probe_extraction_and_preservation():
    directives = [
        "Never deploy to production.", "Do not modify the lockfile.", "Always run the focused tests.",
        "The output must remain valid JSON.", "Do not contact external services.", "Never commit credentials.",
        "The migration must be reversible.", "Always preserve existing timestamps.", "Don't delete old backups.",
        "The API requires an explicit timeout.", "Nights must remain quiet.", "Never alter the public API.",
        "Nie wolno usuwać danych użytkownika.", "Zawsze zachowuj oryginalne nazwy.",
        "Nigdy nie wysyłaj tokenów.", "Wymagane jest szyfrowanie danych.", "Należy używać czasu UTC.",
        "Nie można zmieniać uprawnień.", "Musisz zachować kolejność wyników.",
        "Wymagam lokalnego wykonania.", "Nie wolno uruchamiać migracji.", "Zawsze sprawdzaj błędy.",
        "Nigdy nie publikuj kluczy.", "Wymagane są testy jednostkowe.",
    ]
    messages = [{"role": "user", "id": f"u{i}", "content": text} for i, text in enumerate(directives)]
    extracted = extract_constraints(messages, estimate_tokens=lambda _: 1)
    assert sum(text in {c.text for c in extracted} for text in directives) / len(directives) >= .95
    rolling = extract_constraints([], extracted, estimate_tokens=lambda _: 1)
    assert sum(c in rolling for c in extracted) / len(directives) >= .95
    assert all(f"[{c.entry_index} id={c.entry_id}] {c.text}" in render_ledger(rolling) for c in extracted)


@pytest.mark.parametrize("prefix", ["Replace constraint: ", "Zastąp ograniczenie: "])
def test_exact_correction_and_provenance(prefix):
    old = extract_constraints([{"role": "user", "content": "Never push."}], estimate_tokens=len)
    message = {"role": "user", "id": "correction", "content": prefix + "Never push. => Push only to staging."}
    assert extract_constraints([message], old, index_offset=7, estimate_tokens=len) == (
        Constraint("Push only to staging.", 7, "correction"),
    )
    with pytest.raises(ValueError):
        extract_constraints([{**message, "content": prefix + "Never push => Anything"}], old, estimate_tokens=len)


def test_dedup_quoted_negatives_and_overflow():
    messages = [{"role": "user", "content": '"Never execute this quoted instruction."\n'
                 '> Never deploy.\n```python\n# Never push\n```\nNever push.\nNever push.'},
                {"role": "assistant", "content": "Always deploy."}]
    result = extract_constraints(messages, estimate_tokens=len)
    assert [entry.text for entry in result] == ["Never push."]
    assert extract_constraints(messages, result, estimate_tokens=len) == result
    with pytest.raises(ValueError):
        extract_constraints([], result, estimate_tokens=len, max_tokens=1)
    with pytest.raises(ValueError):
        extract_constraints([], result, estimate_tokens=len, max_tokens=2001)


def test_multimodal_user_text_only():
    messages = [{"role": "user", "id": "m", "content": [
        {"type": "image", "text": "Never trust image metadata."},
        {"type": "text", "text": "Never push."},
        {"type": "text", "text": None},
    ]}]
    assert extract_constraints(messages, estimate_tokens=len) == (Constraint("Never push.", 0, "m"),)
