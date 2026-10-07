from types import SimpleNamespace

import premarket_report


def test_run_reports_missing_premarket_chain_without_crashing(monkeypatch):
    factors = SimpleNamespace(
        nifty_pd=SimpleNamespace(status="OK"),
        india_vix_prev=SimpleNamespace(status="OK"),
        us_direction=lambda: 1,
    )
    collected = {
        "factors": factors,
        "chain": None,
        "fii_dii": {"status": "MISSING"},
        "vix_nse": {"value": None},
        "gift": {"value": None},
        "pdc": None,
    }

    class Store:
        def journal_event(self, _event_type, **_kwargs):
            pass

    monkeypatch.setattr(premarket_report, "setup_logging", lambda: None)
    monkeypatch.setattr(premarket_report, "load_date_set", lambda _path: set())
    monkeypatch.setattr(premarket_report, "collect", lambda _nse: collected)
    monkeypatch.setattr(premarket_report, "build_report",
                        lambda _collected, _event_day: ("report", {}))
    monkeypatch.setattr(premarket_report, "safe_print", lambda _report: None)

    result = premarket_report.run(store=Store(), tg=object(), nse=object(),
                                  send_telegram=False)

    assert result["sources"]["option_chain_premarket"] is False