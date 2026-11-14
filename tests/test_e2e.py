"""End to end: record the task once on the demo site (driven by Playwright
as if it were a person), then replay it for three spreadsheet rows."""
import os

import pytest
from playwright.sync_api import sync_playwright

from demo_server import DemoServer
from webtaskbot import browser
from webtaskbot.automap import automap
from webtaskbot.data import load_table
from webtaskbot.model import Bot
from webtaskbot.recorder import Recorder
from webtaskbot.runner import RunOptions, Runner

CHANNEL = os.environ.get("WTB_TEST_BROWSER")  # None = auto (Chrome > Edge > Chromium)

CSV = (
    "Nombre;Apellido;Email;Departamento;Ingreso;Jornada;CV\n"
    "Ana;López;ana@empresa.com;Tecnología;15/03/2024;Parcial;ana.pdf\n"
    "Luis;Pérez;luis@empresa.com;Ventas;01/04/2024;Completa;luis.pdf\n"
    "Marta;Gil;marta@empresa.com;Recursos Humanos;20/05/2024;Parcial;marta.pdf\n"
)


@pytest.fixture()
def server():
    srv = DemoServer().start()
    yield srv
    srv.stop()


@pytest.fixture()
def workdir(tmp_path):
    (tmp_path / "empleados.csv").write_text(CSV, encoding="utf-8")
    for name in ("ana", "luis", "marta"):
        (tmp_path / f"{name}.pdf").write_bytes(b"%PDF-1.4 demo " + name.encode())
    return tmp_path


def _record(server, workdir):
    def on_dialog(d):
        d.accept("Numero" if d.type == "prompt" else None)

    with sync_playwright() as pw:
        ctx = browser.launch(pw, workdir / ".wtb" / "profiles" / "rec", headless=True, channel=CHANNEL)
        ctx.on("page", lambda p: p.on("dialog", on_dialog))
        rec = Recorder(ctx, server.url + "/form.html")
        rec.page.on("dialog", on_dialog)
        rec.start()
        page = rec.page

        page.get_by_label("Nombre").fill("Ana")          # waits for the late form
        page.get_by_label("Apellido").fill("López")
        page.get_by_placeholder("correo@empresa.com").fill("ana@empresa.com")
        page.get_by_label("Departamento").select_option(label="Tecnología")
        page.get_by_label("Fecha de ingreso").fill("2024-03-15")
        page.get_by_label("Parcial").check()
        page.get_by_label("Contrato firmado").check()
        with page.expect_file_chooser() as fc:
            page.get_by_role("button", name="Adjuntar CV").click()
        fc.value.set_files(str(workdir / "ana.pdf"))
        rec.pump()
        page.frame_locator("#notas").get_by_label("Observaciones").fill("Alta de Ana")
        page.get_by_role("button", name="Guardar").click()
        page.locator("#numero").wait_for()
        rec.pump()
        page.locator("[data-wtb-toolbar]").get_by_role("button", name="Extraer").click()
        rec.pump(300)
        page.locator("#numero").click()
        rec.pump(300)
        steps = rec.finish()
        ctx.close()
    return steps


def test_record_then_replay_three_rows(server, workdir):
    steps = _record(server, workdir)
    assert len(server.submissions) == 1            # the recording itself saved Ana once

    actions = [s["action"] for s in steps]
    assert actions[0] == "goto"
    assert "upload" in actions and "extract" in actions and "select" in actions

    table = load_table(workdir / "empleados.csv")
    bot = Bot("alta", server.url + "/form.html", steps)
    report = automap(bot.steps, table.rows[0], bot.setting("date_format"))
    mapped = {after for _, _, after in report.mapped}
    assert "{{Nombre}}" in mapped and "{{CV}}" in mapped and "Alta de {{Nombre}}" in mapped
    assert "{{Ingreso | date:%Y-%m-%d}}" in mapped
    bot.save(workdir / "bots" / "alta" / "bot.yaml")
    bot = Bot.load(workdir / "bots" / "alta" / "bot.yaml")

    runner = Runner(bot, table, RunOptions(headless=True, channel=CHANNEL, retries=0), workdir, log=print)
    rows = runner.selected_rows()
    assert runner.preflight(rows) == []
    summary = runner.run(rows)
    assert (summary.ok, summary.errors) == (3, 0), summary

    subs = server.submissions[1:]
    assert [s["nombre"] for s in subs] == ["Ana", "Luis", "Marta"]
    assert [s["apellido"] for s in subs] == ["López", "Pérez", "Gil"]
    assert [s["depto"] for s in subs] == ["IT", "VEN", "RH"]
    assert [s["ingreso"] for s in subs] == ["2024-03-15", "2024-04-01", "2024-05-20"]
    assert [s["jornada"] for s in subs] == ["parcial", "completa", "parcial"]
    assert [s["contrato"] for s in subs] == ["si"] * 3
    assert [s["cv"]["filename"] for s in subs] == ["ana.pdf", "luis.pdf", "marta.pdf"]
    assert [s["observaciones"] for s in subs] == ["Alta de Ana", "Alta de Luis", "Alta de Marta"]

    import csv
    rows_out = list(csv.DictReader(
        (summary.run_dir / "resultados.csv").read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))
    assert [r["Numero"] for r in rows_out] == ["EMP-0002", "EMP-0003", "EMP-0004"]
    assert summary.results_xlsx.exists()

    # --resume: everything is already OK, nothing left to do
    runner2 = Runner(bot, table, RunOptions(resume=True, headless=True, channel=CHANNEL), workdir)
    assert runner2.selected_rows() == []


def test_missing_element_fails_row_with_screenshot(server, workdir):
    bot = Bot("roto", server.url + "/form.html", [
        {"action": "goto", "url": server.url + "/form.html"},
        {"action": "click", "target": {"desc": 'botón "No existe"',
                                       "find": [{"role": "button", "name": "No existe"}]}},
    ], settings={"timeout": 3})
    table = load_table(workdir / "empleados.csv")
    runner = Runner(bot, table, RunOptions(rows="1", headless=True, channel=CHANNEL, retries=0), workdir)
    summary = runner.run([1])
    assert (summary.ok, summary.errors) == (0, 1)
    shots = list((summary.run_dir / "capturas").glob("*.png"))
    assert shots, "a screenshot of the failure should be saved"
