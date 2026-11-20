"""Per-item traceability across the whole pipeline."""


def test_trace_unit_through_full_lifecycle(scoped_svc):
    svc = scoped_svc
    plan = svc.plan_packing()
    sealed = svc.sealing.seal(plan)
    stores = {b.sealed_box_id: {"CHILLED": "ST-1", "FROZEN": "ST-2",
                                "DEEP": "ST-3"}[b.zone] for b in sealed}
    tp = svc.plan_transport(stores)
    svc.confirm(tp.plan_id, 1)
    svc.depart(tp.plan_id, 2)

    by_id = {b.sealed_box_id: b for b in sealed}
    for box in sealed:
        for item in box.items:
            entries = svc.trace_unit(item.unit_id)
            assert len(entries) == 1
            e = entries[0]
            assert e["sealed_box_id"] == box.sealed_box_id
            assert e["zone"] == box.zone
            assert e["gross_weight"] == box.gross_weight
            assert e["placement"]["x"] == item.x
            assert e["placement"]["y"] == item.y
            assert e["placement"]["rotated"] == item.rotated
            assert e["item_snapshot"]["weight"] == item.weight
            legs = e["transport"]
            assert len(legs) == 1
            assert legs[0]["plan_id"] == tp.plan_id
            assert legs[0]["status"] == "DEPARTED"
            assert legs[0]["store_id"] == stores[box.sealed_box_id]
            # the shift really carries this box in the plan
            a = next(a for a in svc.get_transport_plan(tp.plan_id).assignments
                     if a.sealed_box_id == box.sealed_box_id)
            assert legs[0]["shift_id"] == a.shift_id


def test_trace_reflects_replacement_history(scoped_svc):
    svc = scoped_svc
    plan = svc.plan_packing()
    sealed = svc.sealing.seal(plan)
    tp = svc.plan_transport({b.sealed_box_id: "ST-1" for b in sealed})
    svc.confirm(tp.plan_id, 1)

    for uid in ("A1", "A2", "B1", "B2"):
        svc.update_item_dims(uid, length=2, width=1)
    new_plan = svc.plan_packing()
    new_sealed = svc.sealing.seal(new_plan)
    svc.replace(tp.plan_id, {b.sealed_box_id: "ST-1" for b in new_sealed},
                expected_version=2)

    entries = svc.trace_unit("A1")
    # A1 appears in the old sealed box and in the new one
    assert len(entries) == 2
    old_entry = next(e for e in entries
                     if e["sealed_box_id"] in
                     {b.sealed_box_id for b in sealed})
    new_entry = next(e for e in entries
                     if e["sealed_box_id"] in
                     {b.sealed_box_id for b in new_sealed})
    assert old_entry["item_snapshot"]["length"] == 2   # old dims preserved
    assert new_entry["item_snapshot"]["length"] == 2
    assert new_entry["item_snapshot"]["width"] == 1    # new dims
    assert old_entry["transport"][0]["status"] == "REPLACED"
    assert new_entry["transport"][0]["status"] == "CONFIRMED"
