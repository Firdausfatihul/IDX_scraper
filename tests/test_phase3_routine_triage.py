from idx_digest.routine_triage import RoutineEvidence, evaluate_routine_disclosure

TITLE = "Laporan Bulanan Registrasi Pemegang Efek"


def test_clean_routine_report_uses_direct_route():
    text = " ".join(["Posisi pemegang saham per 31 Juli 2026 tercatat sesuai daftar registrasi efek."] * 80)
    decision = evaluate_routine_disclosure(TITLE, [RoutineEvidence("main.pdf", text)])
    assert decision.mode == "routine_direct"
    assert decision.signals == ()


def test_routine_report_with_control_change_keeps_full_pipeline():
    text = "Perubahan pemegang saham pengendali terjadi setelah pengalihan saham pada Juli 2026. " * 50
    decision = evaluate_routine_disclosure(TITLE, [RoutineEvidence("main.pdf", text)])
    assert decision.mode == "full"
    assert "control-change" in decision.signals


def test_non_routine_never_uses_direct_route():
    decision = evaluate_routine_disclosure("Akuisisi aset material", [RoutineEvidence("main.pdf", "teks " * 200)])
    assert decision.mode == "full"
