"""cli.py eval / ablate end to end with fake checks: fresh seeded graph, name order, sample 10 env override."""
import fakes
import pytest

import cli


@pytest.fixture
def fake_checks(monkeypatch):
    monkeypatch.setattr("payeeguard.wiring.load_checks", lambda: fakes.FAKE_CHECKS)
    monkeypatch.setattr("payeeguard.wiring.onboard_facts_fn", lambda: fakes.onboard_facts)
    monkeypatch.setattr("payeeguard.wiring.make_client", lambda: None)


def test_cli_eval_all_match(fake_checks, capsys):
    assert cli.main(["eval"]) == 0
    out = capsys.readouterr().out
    assert "10/10 samples match." in out and "3/3 onboarding requests match." in out


def test_cli_eval_is_repeatable(fake_checks, capsys):
    """Each eval starts from a fresh graph, so a second run gives the same table."""
    cli.main(["eval"])
    first = capsys.readouterr().out
    cli.main(["eval"])
    assert capsys.readouterr().out == first


def test_cli_ablate(fake_checks, capsys):
    assert cli.main(["ablate"]) == 0
    out = capsys.readouterr().out
    hero = next(line for line in out.splitlines() if line.startswith("| 06_hero"))
    header = next(line for line in out.splitlines() if line.startswith("| Sample"))
    cols = [c.strip() for c in header.strip("|").split("|")]
    cells = [c.strip() for c in hero.strip("|").split("|")]
    assert cells[cols.index("G3")] == "HOLD"        # without G3 the hero is a HOLD: the graph decides it


def test_cli_gate_commands(fake_checks, capsys, tmp_path, graph, monkeypatch):
    monkeypatch.setattr("payeeguard.wiring.open_graph", lambda path=None: graph)
    f = tmp_path / "in.txt"
    f.write_text(fakes.sample("04_new_account_real_domain.txt"), encoding="utf-8")
    cli.main(["verify", str(f)])
    case = capsys.readouterr().out.split("case ")[1].split()[0]
    cli.main(["pay", case])
    assert "Refused" in capsys.readouterr().out
    cli.main(["resolve", case, "approve", "--by", "asha", "--note", "called vendor"])
    assert "approved" in capsys.readouterr().out
    cli.main(["status", case])
    assert "status APPROVED" in capsys.readouterr().out
