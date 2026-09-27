# Sphinx-документация

Исходники сайта https://docs.mowtransit.ru: инструкция для жюри (`getting_started.rst`), ML, API ML-ядра, backend и
справочник по коду (autodoc по `ml/src` и `backend/app`).

## Сборка

```bash
pip install sphinx sphinx-rtd-theme
make -C docs/sphinx html        # → docs/sphinx/_build/html/index.html
```

На сервере сайт собирается в `/var/www/mowtransit-docs` — команда в [`infra/deploy/README.md`](../../infra/deploy/README.md), раздел «Docs».
