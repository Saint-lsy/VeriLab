from __future__ import annotations

from verilab.bundle import AuditBundle


def test_target_bundle_digest_is_an_allowed_evidence_reference(tmp_path) -> None:
    bundle_sha, event_refs, sha_refs = AuditBundle(tmp_path / "bundle").build(
        experiment={},
        run={},
        policy={},
        artifacts=[{"sha256": "a" * 64}],
        metrics=[],
        events=[{"seq": 7}],
    )

    assert f"sha256:{bundle_sha}" in sha_refs
    assert "sha256:" + "a" * 64 in sha_refs
    assert event_refs == {"event:7"}
