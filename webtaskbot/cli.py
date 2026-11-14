"""wtb — graba una tarea web una vez y repítela para cada fila de un Excel.

    wtb record alta-empleados --url https://intranet/empleados/nuevo --data empleados.xlsx
    wtb check  alta-empleados --data empleados.xlsx
    wtb run    alta-empleados --data empleados.xlsx --rows 1 --step
    wtb run    alta-empleados --data empleados.xlsx
    wtb run    alta-empleados --data empleados.xlsx --resume
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

from webtaskbot import __version__
from webtaskbot.ui import ask_yes_no, c


def _home(args) -> Path:
    return Path(args.home or os.environ.get("WTB_HOME") or Path.cwd()).resolve()


def _bot_path(home: Path, name: str) -> Path:
    return home / "bots" / name / "bot.yaml"


def _valid_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}", name):
        raise argparse.ArgumentTypeError(
            "usa letras, números, guiones o puntos (sin espacios), p.ej. alta-empleados"
        )
    return name


def _load_bot(home: Path, name: str):
    from webtaskbot.model import Bot

    path = _bot_path(home, name)
    if not path.exists():
        available = sorted(p.parent.name for p in (home / "bots").glob("*/bot.yaml"))
        hint = f" Bots disponibles: {', '.join(available)}" if available else ""
        raise SystemExit(c("red", f"No existe el bot '{name}' ({path}).{hint}"))
    return Bot.load(path)


def _load_table(args):
    from webtaskbot.data import load_table

    return load_table(args.data, getattr(args, "sheet", None))


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------
def cmd_record(args) -> int:
    from playwright.sync_api import sync_playwright

    from webtaskbot import browser
    from webtaskbot.automap import automap
    from webtaskbot.model import Bot, DEFAULT_SETTINGS, describe_step
    from webtaskbot.recorder import Recorder, print_step

    home = _home(args)
    path = _bot_path(home, args.name)
    if path.exists() and not args.force:
        if not ask_yes_no(f"Ya existe el bot '{args.name}'. ¿Sobrescribirlo?"):
            return 1

    table = _load_table(args) if args.data else None
    if table is not None:
        if not table.rows:
            raise SystemExit(c("red", "El archivo de datos no tiene filas."))
        first = table.rows[0]
        print(c("bold", "\nGraba la tarea usando los datos del PRIMER registro:"))
        for col, val in first.items():
            from webtaskbot.template import format_cell
            print(f"    {col:<20} {format_cell(val)}")
        print(c("dim", "  (así el bot sabrá qué columna va en cada campo)"))

    print(c("bold", "\nSe abrirá el navegador. Haz la tarea normalmente, una sola vez."))
    print("  · La barra abajo a la derecha tiene: Esperar, Verificar, Extraer, Pausar y Terminar.")
    print("  · Si hace falta iniciar sesión y no quieres que se grabe, pulsa Pausar antes.")
    print("  · Al acabar pulsa Terminar (o cierra el navegador, o Ctrl+C aquí).\n")

    url = args.url
    if not re.match(r"^[a-z]+://", url):
        url = "https://" + url

    with sync_playwright() as pw:
        ctx = browser.launch(pw, home / ".wtb" / "profiles" / args.profile, channel=args.browser)
        rec = Recorder(ctx, url, on_step=print_step)
        steps = rec.run()
        try:
            ctx.close()
        except Exception:  # noqa: BLE001 - browser may already be gone
            pass

    if len(steps) <= 1:
        print(c("yellow", "No se grabó ninguna acción; no guardo el bot."))
        return 1

    bot = Bot(name=args.name, start_url=url, steps=steps, settings=dict(DEFAULT_SETTINGS))
    if table is not None:
        report = automap(bot.steps, table.rows[0], bot.setting("date_format"))
        if report.mapped:
            print(c("bold", "\nValores conectados a columnas del Excel:"))
            for i, before, after in report.mapped:
                print(f"  paso {i:>3}: {before!r} → {c('green', after)}")
        if report.literal:
            print(c("bold", "\nValores fijos (iguales en todos los registros):"))
            for i, value in report.literal:
                print(f"  paso {i:>3}: {value!r}")
        if report.ambiguous:
            print(c("yellow", "\nValores que coinciden con varias columnas (los dejé fijos, revísalos):"))
            for i, value, cols in report.ambiguous:
                print(f"  paso {i:>3}: {value!r} podría ser {', '.join(cols)}")
        bot.settings["files_dir"] = None
    saved = bot.save(path)
    secrets = bot.required_secrets()
    print(c("green", f"\nBot guardado en {saved}  ({len(bot.steps)} pasos)"))
    if secrets:
        print(f"  Contraseñas detectadas (no se guardan): {', '.join(secrets)}. "
              f"Se pedirán al ejecutar o define WTB_SECRET_<NOMBRE>.")
    data_arg = f" --data {args.data}" if args.data else " --data TUS_DATOS.xlsx"
    print("\nSiguientes pasos:")
    print(f"  wtb show {args.name}")
    print(f"  wtb check {args.name}{data_arg}")
    print(f"  wtb run {args.name}{data_arg} --rows 1 --step   (prueba paso a paso)")
    print(f"  wtb run {args.name}{data_arg}")
    return 0


# --------------------------------------------------------------------------
# show / list / edit
# --------------------------------------------------------------------------
def cmd_show(args) -> int:
    from webtaskbot.model import describe_step

    bot = _load_bot(_home(args), args.name)
    print(c("bold", f"{bot.name}") + f"  ({bot.path})")
    print(f"  inicio: {bot.start_url}")
    for i, step in enumerate(bot.steps, 1):
        flags = []
        if step.get("optional"):
            flags.append("opcional")
        if step.get("tab"):
            flags.append(f"pestaña {step['tab'] + 1}")
        if step.get("target", {}).get("frames"):
            flags.append("dentro de iframe")
        extra = c("dim", f"  [{', '.join(flags)}]") if flags else ""
        print(f"  {i:>3}. {describe_step(step)}{extra}")
    cols = bot.required_columns()
    if cols:
        print(f"\n  Columnas que usa: {', '.join(cols)}")
    if bot.extract_columns():
        print(f"  Columnas que añade a los resultados: {', '.join(bot.extract_columns())}")
    if bot.required_secrets():
        print(f"  Secretos: {', '.join(bot.required_secrets())}")
    return 0


def cmd_list(args) -> int:
    from webtaskbot.model import Bot, BotError

    home = _home(args)
    paths = sorted((home / "bots").glob("*/bot.yaml"))
    if not paths:
        print("No hay bots todavía. Graba uno con:  wtb record NOMBRE --url URL --data DATOS.xlsx")
        return 0
    for p in paths:
        try:
            bot = Bot.load(p)
            print(f"  {bot.name:<28} {len(bot.steps):>3} pasos   {bot.start_url}")
        except BotError as exc:
            print(f"  {p.parent.name:<28} {c('red', 'con errores')}: {str(exc).splitlines()[0]}")
    return 0


def cmd_edit(args) -> int:
    path = _bot_path(_home(args), args.name)
    if not path.exists():
        raise SystemExit(c("red", f"No existe {path}"))
    print(f"Abriendo {path}")
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        editor = os.environ.get("EDITOR", "nano")
        os.system(f'{editor} "{path}"')
    return 0


# --------------------------------------------------------------------------
# check / run
# --------------------------------------------------------------------------
def _make_runner(args):
    from webtaskbot.runner import RunOptions, Runner

    home = _home(args)
    bot = _load_bot(home, args.name)
    table = _load_table(args)
    opts = RunOptions(
        rows=getattr(args, "rows", None),
        resume=getattr(args, "resume", False),
        retries=getattr(args, "retries", 1),
        headless=getattr(args, "headless", False),
        slow_mo=getattr(args, "slow", 0),
        step_mode=getattr(args, "step", False),
        stop_on_error=getattr(args, "stop_on_error", False),
        files_dir=Path(args.files_dir) if getattr(args, "files_dir", None) else None,
        channel=getattr(args, "browser", None),
        profile=getattr(args, "profile", "default"),
    )
    return Runner(bot, table, opts, home), bot, table


def cmd_check(args) -> int:
    runner, bot, table = _make_runner(args)
    rows = runner.selected_rows()
    print(f"Bot '{bot.name}': {len(bot.steps)} pasos. Datos: {table.path.name} ({len(table)} registros, "
          f"{len(rows)} seleccionados)")
    problems = runner.preflight(rows)
    if problems:
        print(c("red", f"\n{len(problems)} problema(s):"))
        for p in problems[:50]:
            print(f"  · {p}")
        if len(problems) > 50:
            print(f"  … y {len(problems) - 50} más")
        return 1
    print(c("green", "Todo en orden: columnas, plantillas y archivos a subir."))
    return 0


def cmd_run(args) -> int:
    runner, bot, table = _make_runner(args)
    rows = runner.selected_rows()
    if args.resume:
        done = len(runner.previous_ok_rows())
        print(f"Reanudando: {done} registro(s) ya estaban OK en ejecuciones anteriores.")
    if not rows:
        print(c("green", "No hay registros pendientes."))
        return 0
    problems = runner.preflight(rows)
    if problems and not args.ignore_problems:
        print(c("red", f"Antes de empezar encontré {len(problems)} problema(s):"))
        for p in problems[:30]:
            print(f"  · {p}")
        print("\nCorrígelos o ejecuta con --ignore-problems (esos registros fallarán).")
        return 1

    print(c("bold", f"Ejecutando '{bot.name}' sobre {len(rows)} registro(s) de {table.path.name}"))
    print(c("dim", "  Ctrl+C para parar; luego puedes seguir con --resume\n"))
    summary = runner.run(rows)

    print()
    print(c("bold", "Resumen: ") + c("green", f"{summary.ok} OK") + "  " +
          (c("red", f"{summary.errors} con error") if summary.errors else "0 con error") +
          (f"  {summary.skipped} omitidos" if summary.skipped else ""))
    print(f"  Resultados: {summary.results_xlsx}")
    if summary.errors:
        print(f"  Capturas de los errores en: {summary.run_dir / 'capturas'}")
    if summary.errors or summary.interrupted:
        print(f"  Para reintentar solo lo pendiente:  wtb run {bot.name} --data {args.data} --resume")
    return 0 if not summary.errors and not summary.interrupted else 2


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wtb",
        description="Graba una tarea en una página web y repítela para cada fila de un Excel/CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2],
    )
    p.add_argument("--version", action="version", version=f"wtb {__version__}")
    p.add_argument("--home", help="carpeta de trabajo (bots, ejecuciones, perfil). Por defecto, la actual")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMANDO")

    def browser_opts(sp):
        sp.add_argument("--browser", choices=["chrome", "msedge", "chromium"],
                        help="navegador a usar (por defecto: Chrome, si no Edge, si no Chromium)")
        sp.add_argument("--profile", default="default",
                        help="perfil del navegador (sesiones guardadas). Por defecto: default")

    r = sub.add_parser("record", help="grabar un bot nuevo haciendo la tarea una vez")
    r.add_argument("name", type=_valid_name, help="nombre del bot, p.ej. alta-empleados")
    r.add_argument("--url", required=True, help="página donde empieza la tarea")
    r.add_argument("--data", help="Excel/CSV: graba con la 1.ª fila y se conectan las columnas solas")
    r.add_argument("--sheet", help="hoja del Excel (por defecto la primera)")
    r.add_argument("--force", action="store_true", help="sobrescribir sin preguntar")
    browser_opts(r)
    r.set_defaults(func=cmd_record)

    s = sub.add_parser("show", help="ver los pasos de un bot")
    s.add_argument("name")
    s.set_defaults(func=cmd_show)

    ls = sub.add_parser("list", help="listar los bots")
    ls.set_defaults(func=cmd_list)

    e = sub.add_parser("edit", help="abrir el archivo del bot para retocarlo")
    e.add_argument("name")
    e.set_defaults(func=cmd_edit)

    def data_opts(sp):
        sp.add_argument("name")
        sp.add_argument("--data", required=True, help="Excel (.xlsx) o CSV con un registro por fila")
        sp.add_argument("--sheet", help="hoja del Excel (por defecto la primera)")
        sp.add_argument("--rows", help="registros a procesar: 1-10,15,20-  (1 = primera fila de datos)")
        sp.add_argument("--resume", action="store_true",
                        help="saltar los registros que ya salieron OK en ejecuciones anteriores")
        sp.add_argument("--files-dir", help="carpeta de los archivos a subir (por defecto, la del Excel)")

    ch = sub.add_parser("check", help="comprobar datos, columnas y archivos sin abrir el navegador")
    data_opts(ch)
    ch.set_defaults(func=cmd_check)

    run = sub.add_parser("run", help="ejecutar el bot para cada registro")
    data_opts(run)
    run.add_argument("--retries", type=int, default=1, help="reintentos por registro (por defecto 1)")
    run.add_argument("--headless", action="store_true", help="sin ventana del navegador")
    run.add_argument("--slow", type=int, default=0, metavar="MS", help="pausa entre acciones (ms)")
    run.add_argument("--step", action="store_true", help="paso a paso: pide Enter antes de cada paso")
    run.add_argument("--stop-on-error", action="store_true", help="parar en el primer error")
    run.add_argument("--ignore-problems", action="store_true",
                     help="ejecutar aunque la comprobación previa encuentre problemas")
    browser_opts(run)
    run.set_defaults(func=cmd_run)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    from webtaskbot.data import DataError
    from webtaskbot.model import BotError

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (DataError, BotError) as exc:
        print(c("red", f"Error: {exc}"))
        return 1
    except KeyboardInterrupt:
        print(c("yellow", "\nCancelado."))
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort friendly message
        from webtaskbot.browser import BrowserLaunchError

        if isinstance(exc, BrowserLaunchError):
            print(c("red", str(exc)))
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
