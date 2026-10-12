import shutil
from pathlib import Path

import pytest
import yaml

from misecre.config import expand_dotted, load_config

ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path, extra: dict | None = None, text_extra: str = ""):
    raw = yaml.safe_load((ROOT / "config/config.example.yaml").read_text(encoding="utf-8"))
    raw["data_dir"] = str(tmp_path / "data")
    raw.update(extra or {})
    (tmp_path / "config").mkdir(exist_ok=True)
    shutil.copy(ROOT / "config/rules.yaml", tmp_path / "config/rules.yaml")
    p = tmp_path / "config/config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True) + text_extra, encoding="utf-8")
    return p


def test_clave_con_puntos_se_expande_como_anidada(tmp_path):
    """`ai.enabled: true` en una sola línea es el error típico: tiene que funcionar igual."""
    p = write(tmp_path, {"ai": {"model": "claude-haiku-5-5"}}, "ai.enabled: true\napi.port: 9999\n")
    cfg = load_config(p)
    assert cfg.ai.enabled is True and cfg.ai.model == "claude-haiku-5-5"   # se fusionó con la sección
    assert cfg.api.port == 9999
    assert cfg.unknown_keys == []


def test_expand_dotted_no_pisa_hermanos():
    out = expand_dotted({"a": {"x": 1}, "a.y": 2, "b.c.d": 3})
    assert out == {"a": {"x": 1, "y": 2}, "b": {"c": {"d": 3}}}


def test_claves_desconocidas_se_reportan(tmp_path):
    p = write(tmp_path, {"ia": {"enabled": True}, "ai": {"enable": True, "enabled": False}})
    cfg = load_config(p)
    assert set(cfg.unknown_keys) == {"ia", "ai.enable"}
    assert cfg.ai.enabled is False                                     # y no se activó por accidente


def test_claves_de_versiones_viejas_no_molestan(tmp_path):
    p = write(tmp_path, {"vault": {"path": "~/x", "root_folder": "M"},
                         "sync": {"adopt_notes": True}, "digests": {"daily_note_folder": "Diario"}})
    assert load_config(p).unknown_keys == []


def test_config_de_ejemplo_no_tiene_claves_desconocidas(tmp_path):
    assert load_config(write(tmp_path)).unknown_keys == []


def test_encuentra_la_config_desde_cualquier_carpeta(tmp_path, monkeypatch):
    import misecre.config as C

    proyecto = tmp_path / "proyecto"
    (proyecto / "config").mkdir(parents=True)
    (proyecto / "config" / "config.yaml").write_text("x: 1")
    otra = tmp_path / "otra" / "carpeta"
    otra.mkdir(parents=True)
    monkeypatch.chdir(otra)                                         # parado en una carpeta cualquiera
    monkeypatch.delenv("MISECRE_CONFIG", raising=False)
    monkeypatch.setattr(C, "_candidates", lambda: [Path.cwd() / "config" / "config.yaml",
                                                  proyecto / "config" / "config.yaml"])
    assert C.find_config() == proyecto / "config" / "config.yaml"   # cae a la carpeta del proyecto


def test_prioridad_explicita_y_error_claro(tmp_path, monkeypatch):
    import misecre.config as C

    monkeypatch.delenv("MISECRE_CONFIG", raising=False)
    monkeypatch.setattr(C, "_candidates", lambda: [tmp_path / "nada" / "config.yaml"])
    with pytest.raises(FileNotFoundError) as e:
        C.find_config()
    assert "No encuentro config.yaml" in str(e.value) and "cp config/config.example.yaml" in str(e.value)
    with pytest.raises(FileNotFoundError, match="No existe el archivo"):
        C.find_config(tmp_path / "otro.yaml")
    f = tmp_path / "mi.yaml"
    f.write_text("a: 1")
    monkeypatch.setenv("MISECRE_CONFIG", str(f))
    assert C.find_config() == f
