from concurrent.futures import ThreadPoolExecutor

import pytest

from core.crypto_store import CryptoError, CryptoStore
from core.database import StudentStore
from core.mentor_practice import (
    MODE_NAMES, MODE_PHOTOS, MODE_REVIEW, ROUND_LENGTH, new_round, regular_targets, review_targets,
)
from core.sensitive_vault import SensitiveVault
from core.student_deletion import purge_student
from core.workbench_cleanup import clear_all_student_data, clear_inventory


@pytest.fixture
def account(tmp_path):
    store = CryptoStore(tmp_path, iterations=2000)
    user, password = "synthetic-mentor", "Only-for-tests!1"
    store.register(user, password)
    roster = StudentStore(store=store, username=user)
    for sid, name in (("TEST0001", "测试甲"), ("TEST0002", "测试乙"), ("TEST0003", "测试丙")):
        roster.add(sid, name, "测试班")
    vault = SensitiveVault(store, user)
    vault.unlock(password)
    return store, roster, vault


@pytest.mark.parametrize("count", [2, 4, 20, 23])
def test_regular_round_has_twenty_without_repeating_until_pool_ends(count):
    pool = [str(i) for i in range(count)]
    targets = regular_targets(pool, MODE_NAMES)
    assert len(targets) == ROUND_LENGTH
    assert {mode for _, mode in targets} == {MODE_NAMES}
    ids = [sid for sid, _ in targets]
    assert all(a != b for a, b in zip(ids, ids[1:]))
    for offset in range(0, ROUND_LENGTH, count):
        group = ids[offset:offset + count]
        assert len(group) == len(set(group))
    assert regular_targets(["1"], MODE_NAMES) == []


def test_wrong_answer_queue_tracks_direction_and_account(account):
    store, roster, vault = account
    round_ = new_round("测试班", MODE_NAMES, ROUND_LENGTH)
    vault.update("TEST0001", lambda data: data.update(practice={"attempts": 5, "correct": 2}))
    assert vault.practice_rounds() == []
    vault.record_practice_answer(round_, 1, MODE_NAMES, "sid:TEST0001", "TEST0002")
    photo_round = new_round("测试班", MODE_PHOTOS, ROUND_LENGTH)
    vault.record_practice_answer(photo_round, 1, MODE_PHOTOS, "TEST0001", "TEST0003")
    ref = vault.student("TEST0001")["practice_ref"]
    assert set(review_targets(vault.practice_rounds(), {ref: "TEST0001"})) == {
        ("TEST0001", MODE_NAMES), ("TEST0001", MODE_PHOTOS),
    }
    retry = new_round("测试班", MODE_REVIEW, 1)
    vault.record_practice_answer(retry, 1, MODE_NAMES, "TEST0001", "TEST0001")
    assert review_targets(vault.practice_rounds(), {ref: "TEST0001"}) == [("TEST0001", MODE_PHOTOS)]
    assert review_targets(vault.practice_rounds(), {}) == []
    assert clear_inventory(store, vault.username, vault)["练习记录"] == 8

    store.register("other-mentor", "Only-for-tests!2")
    other = SensitiveVault(store, "other-mentor")
    other.unlock("Only-for-tests!2")
    assert other.practice_rounds() == []
    vault.lock()
    with pytest.raises(CryptoError):
        vault.practice_rounds()


def test_partial_round_saved_duplicate_answer_idempotent_and_refs_delete_cleanly(account):
    store, roster, vault = account
    round_ = new_round("测试班", MODE_NAMES, ROUND_LENGTH)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writes = [pool.submit(vault.record_practice_answer, round_, 1, MODE_NAMES, "TEST0001", "TEST0002")
                  for _ in range(2)]
        first, second = [write.result() for write in writes]
    assert first == second
    persisted, = vault.practice_rounds()
    assert len(persisted["answers"]) == 1 and persisted["total"] == 20
    answer, = persisted["answers"]
    target_ref, chosen_ref = answer["target"], answer["selected"]
    assert target_ref != chosen_ref and not answer["correct"]
    assert "TEST0001" not in str(persisted) and "测试甲" not in str(persisted)
    assert len(list((store.user_dir(roster.uid)/"sensitive/practice").glob("*.enc"))) == 1

    vault.move_student("TEST0001", "TEST1001")
    assert vault.student("TEST1001")["practice_ref"] == target_ref
    assert purge_student(store, vault.username, "TEST1001")
    roster.add("TEST1001", "替换测试生", "测试班")
    assert vault.student("TEST1001").get("practice_ref") is None
    assert vault.student("TEST0002")["practice_ref"] == chosen_ref
    with pytest.raises(CryptoError):
        vault.record_practice_answer(round_, 3, MODE_NAMES, "TEST1001", "TEST0002")

    result = clear_all_student_data(store, vault.username)
    assert not result["failed"]
    assert vault.practice_rounds() == []
