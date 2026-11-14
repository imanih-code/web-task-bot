import datetime as dt

import pytest

from webtaskbot import template
from webtaskbot.automap import automap, map_file, map_value
from webtaskbot.data import load_table, parse_row_selection
from webtaskbot.model import Bot, BotError

ROW = {
    "Nombre": "Ana",
    "Apellido": "López",
    "DNI": 12345678.0,
    "Ingreso": dt.datetime(2024, 3, 15),
    "Jornada": "Parcial",
}


# ------------------------------------------------------------------ template
def test_render_basic_and_filters():
    assert template.render("{{Nombre}} {{ apellido | upper }}", ROW) == "Ana LÓPEZ"
    assert template.render("{{DNI}}", ROW) == "12345678"          # no '.0'
    assert template.render("{{Ingreso}}", ROW) == "15/03/2024"
    assert template.render("{{Ingreso | date:%Y-%m-%d}}", ROW) == "2024-03-15"
    assert template.render("{{Telefono | default:-}}", {"Telefono": ""}) == "-"


def test_render_date_filter_on_text_cell():
    assert template.render("{{F | date:%Y-%m-%d}}", {"F": "01/04/2024"}) == "2024-04-01"


def test_render_unknown_column_lists_available():
    with pytest.raises(template.TemplateError, match="Columnas disponibles"):
        template.render("{{Sueldo}}", ROW)


def test_secrets_from_env(monkeypatch):
    monkeypatch.setenv("WTB_SECRET_CLAVE", "s3creta")
    secrets = template.Secrets(ask=lambda n: pytest.fail("should not ask"))
    assert template.render("{{secret:CLAVE}}", ROW, secrets) == "s3creta"


# -------------------------------------------------------------------- automap
def test_map_value_exact_partial_and_dates():
    assert map_value("Ana", ROW, "%d/%m/%Y")[0] == "{{Nombre}}"
    assert map_value("ana lópez", ROW, "%d/%m/%Y")[0] == "{{Nombre}} {{Apellido}}"
    assert map_value("Alta de Ana", ROW, "%d/%m/%Y")[0] == "Alta de {{Nombre}}"
    assert map_value("2024-03-15", ROW, "%d/%m/%Y")[0] == "{{Ingreso | date:%Y-%m-%d}}"
    assert map_value("Banana", ROW, "%d/%m/%Y")[0] == "Banana"     # no match inside words


def test_map_file_uses_stem():
    assert map_file("12345678.pdf", ROW, "%d/%m/%Y") == "{{DNI}}.pdf"


def test_automap_click_on_data_driven_option():
    steps = [{
        "action": "check",
        "target": {"desc": 'opción "Parcial"', "find": [
            {"id": "jornada-parcial"},
            {"role": "radio", "name": "Parcial"},
            {"label": "Parcial"},
            {"css": 'input[name="jornada"][value="parcial"]'},
        ]},
    }, {
        "action": "click",
        "target": {"desc": 'botón "Guardar"', "find": [{"role": "button", "name": "Guardar"}]},
    }]
    report = automap(steps, ROW, "%d/%m/%Y")
    find = steps[0]["target"]["find"]
    assert find == [{"role": "radio", "name": "{{Jornada}}"}, {"label": "{{Jornada}}"}]
    assert steps[1]["target"]["find"] == [{"role": "button", "name": "Guardar"}]
    assert len(report.mapped) == 1


def test_automap_select_prefers_label_and_drops_row1_value():
    steps = [{"action": "select", "target": {"desc": "x", "find": [{"css": "#d"}]},
              "value": "VEN", "label": "Parcial"}]
    automap(steps, ROW, "%d/%m/%Y")
    assert steps[0]["label"] == "{{Jornada}}"
    assert "value" not in steps[0]


# ----------------------------------------------------------------------- data
def test_csv_semicolon_latin1(tmp_path):
    p = tmp_path / "d.csv"
    p.write_bytes("Nombre;Apellido\nAna;López\n\nLuis;Pérez\n".encode("cp1252"))
    t = load_table(p)
    assert t.columns == ["Nombre", "Apellido"]
    assert [r["Apellido"] for r in t.rows] == ["López", "Pérez"]


def test_xlsx_types(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Nombre", "DNI", "Ingreso", None])
    ws.append(["Ana", 12345678, dt.datetime(2024, 3, 15), None])
    ws.append([None, None, None, None])
    ws.append(["Luis", 87654321, dt.datetime(2024, 4, 1), None])
    p = tmp_path / "d.xlsx"
    wb.save(p)
    t = load_table(p)
    assert t.columns == ["Nombre", "DNI", "Ingreso"]
    assert len(t) == 2
    assert template.render("{{DNI}} {{Ingreso}}", t.rows[1]) == "87654321 01/04/2024"


def test_row_selection():
    assert parse_row_selection("1-3,5,9-", 10) == [1, 2, 3, 5, 9, 10]
    assert parse_row_selection(None, 3) == [1, 2, 3]
    assert parse_row_selection("2,2,50", 5) == [2]


# ---------------------------------------------------------------------- model
def test_bot_roundtrip_and_validation(tmp_path):
    bot = Bot("b", "http://x", [
        {"action": "goto", "url": "http://x"},
        {"action": "fill", "target": {"desc": "campo", "find": [{"css": "#a"}]}, "value": "{{Nombre}}"},
        {"action": "click", "target": {"desc": "b", "find": [{"role": "button", "name": "{{Jornada}}"}]}},
    ])
    path = bot.save(tmp_path / "bot.yaml")
    loaded = Bot.load(path)
    assert loaded.steps == bot.steps
    assert loaded.required_columns() == ["Nombre", "Jornada"]

    path.write_text("name: b\nsteps:\n  - action: volar\n  - action: fill\n", encoding="utf-8")
    with pytest.raises(BotError) as err:
        Bot.load(path)
    assert "volar" in str(err.value) and "falta 'target'" in str(err.value)
