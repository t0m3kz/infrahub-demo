#!/bin/bash

poetry config virtualenvs.create true
poetry install --no-interaction --no-ansi

poetry run invoke start
