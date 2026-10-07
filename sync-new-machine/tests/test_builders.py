from __future__ import annotations

from revend_sync import builders

from .fake_api import MACHINE_ID, check_transactions


def row(id, dateline, **overrides):
    """A user_transaction row as real machines write it (October 2026 sample)."""
    base = {
        "id": id,
        "transactionid": "2602530598507000000000000001",
        "dateline": dateline,
        "statecode": "1",
        "barcode": "5901234123457",
        "bors": "unknown",
        "weight": "18",
        "diam": "0",
        "metal": "0",
        "recognitionstatus": "1",
        "print_barcode": "2602530598507",
        "payplatform": "RVM",
        "bottlevalue": "5",
        "transactiondone": 2,
    }
    base.update(overrides)
    return base


def test_a_real_transaction_becomes_a_valid_v2_transaction():
    rows = [
        row(10, 1790000000),
        row(11, 1790000004, metal="1", weight="13"),
        row(12, 1790000009, recognitionstatus="33", metal=None, barcode="non", bottlevalue="5"),
        row(13, 1790000012, recognitionstatus="6", metal=None),
    ]
    built = builders.build_transaction("2602530598507", rows)
    t = built.value

    assert built.problems == []
    assert t["couponId"] == "2602530598507"
    assert t["transactionId"] == "2602530598507000000000000001"
    assert (t["startedAt"], t["finishedAt"]) == ("2026-09-21T14:13:20Z", "2026-09-21T14:13:32Z")
    assert (t["acceptedItemsCount"], t["rejectedItemsCount"], t["totalDepositAmount"]) == (2, 2, 100)
    assert [d["materialType"] for d in t["details"]] == ["pet", "can", None, None]
    assert [d["depositAmount"] for d in t["details"]] == [50, 50, 0, 0]
    assert t["details"][2]["barcode"] is None
    assert t["details"][2]["recognitionStatus"] == 33
    assert t["details"][1]["weightGrams"] == 13
    assert check_transactions({"data": {"transactions": [t]}}) is None


def test_an_accepted_item_without_material_is_sent_as_pet_and_reported():
    built = builders.build_transaction("C1", [row(1, 1790000000, metal=None)])
    assert built.value["details"][0]["materialType"] == "pet"
    assert "no material" in built.problems[0]


def test_deposit_values_in_any_machine_unit_become_grosze():
    assert [builders.deposit_grosze(v) for v in ("0.5", "5", "50", 1, "10", "0", None, "x")] == [
        50,
        50,
        50,
        100,
        100,
        0,
        50,
        50,
    ]


def test_batches_respect_the_api_limits():
    many = [builders.build_transaction(f"C{i}", [row(i, 1790000000)]).value for i in range(250)]
    batches = builders.transaction_batches(MACHINE_ID, many)
    assert [len(b["data"]["transactions"]) for b in batches] == [120, 120, 10]

    big = [
        builders.build_transaction(f"B{i}", [row(i * 1000 + j, 1790000000 + j) for j in range(400)]).value
        for i in range(10)
    ]
    batches = builders.transaction_batches(MACHINE_ID, big)
    assert all(sum(len(t["details"]) for t in b["data"]["transactions"]) <= 3000 for b in batches)
    assert sum(len(b["data"]["transactions"]) for b in batches) == 10
    assert all("sentAt" not in b for b in batches)


def test_unchanged_transactions_have_the_same_fingerprint():
    a = builders.build_transaction("C", [row(1, 1790000000)]).value
    b = builders.build_transaction("C", [row(1, 1790000000)]).value
    c = builders.build_transaction("C", [row(1, 1790000000), row(2, 1790000001)]).value
    assert builders.fingerprint(a) == builders.fingerprint(b) != builders.fingerprint(c)


def test_bin_sides_map_to_materials():
    left = builders.build_bin(
        MACHINE_ID, {"id": 1, "barcode": " 700012109712345 ", "bin_type": "left", "dateline": 1790000000}
    )
    right = builders.build_bin(
        MACHINE_ID, {"id": 2, "barcode": "SEAL2", "bin_type": "right", "dateline": 1790000000}
    )
    odd = builders.build_bin(
        MACHINE_ID, {"id": 3, "barcode": "SEAL3", "bin_type": "MIX", "dateline": 1790000000}
    )
    broken = builders.build_bin(
        MACHINE_ID, {"id": 4, "barcode": "", "bin_type": "left", "dateline": 1790000000}
    )

    assert left.value["data"]["empty_records"] == [
        {"barcode": "700012109712345", "bin_type": "pet", "dateline": 1790000000}
    ]
    assert right.value["data"]["empty_records"][0]["bin_type"] == "can"
    assert odd.value["data"]["empty_records"][0]["bin_type"] == "pet" and odd.problems
    assert broken.value is None and broken.problems


def test_status_uses_free_space_values_and_a_short_error_code():
    payload = builders.build_status(
        MACHINE_ID,
        {"id": 9, "storage": 120, "storageplastic": "40", "storagecan": -3, "errorcode": "E123456789"},
        1790000000,
    )
    assert payload["data"]["command"] == {
        "storage": 100,
        "storageplastic": 40,
        "storagecan": 0,
        "errorcode": "E1234567",
    }
    assert payload["kind"] == "status"
