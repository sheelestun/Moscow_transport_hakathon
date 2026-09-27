# Configuration file for the Sphinx documentation builder.
#
# Полная документация по опциям:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import os
import sys

# -- Path setup --------------------------------------------------------------
# Чтобы autodoc нашёл ML- и backend-модули, добавляем оба пути.
sys.path.insert(0, os.path.abspath("../../ml/src"))
sys.path.insert(0, os.path.abspath("../../backend"))  # пакет app (боевой бэкенд)

# -- Project information -----------------------------------------------------
project = "Предиктор задержек Москвы"
author = "Команда Moscow Transport Hakaton"
copyright = "2026"

# -- General configuration ---------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.autosummary",
]

# autodoc/autosummary
autosummary_generate = True
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "special-members": "__init__",
}

# Napoleon (Google + NumPy стили docstring-ов, у нас смешанные)
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_use_param = True
napoleon_use_rtype = True

# Не падать при отсутствии тяжёлых зависимостей
autodoc_mock_imports = [
    "numpy",
    "pandas",
    "catboost",
    "onnxruntime",
    "onnx",
    "sklearn",
    "torch",
    "fastapi",
    "pydantic",
    "uvicorn",
    "orjson",
    "geopy",
    "haversine",
    "tqdm",
    "yaml",
    "matplotlib",
    "seaborn",
    "scipy",
    "joblib",
    "httpx",
    "asyncpg",
    "pydantic_settings",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

master_doc = "index"
language = "ru"

# -- Options for HTML output -------------------------------------------------
html_theme = "sphinx_rtd_theme"
html_title = "Предиктор задержек Москвы"

# Не падать на предупреждениях от viewcode для замоканных модулей
suppress_warnings = ["autosummary", "autosummary.import_cycle"]
