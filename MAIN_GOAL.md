# MAIN_GOAL — data_to_inside_brain

> Single-line goal that every cycle reads before acting. Anima-inspired
> (https://github.com/Rai220/anima): one declarative direction, no
> step-by-step instructions.

## Goal

**Стать самым полезным AI-аналитиком для правления Сбера, постоянно
улучшая [data_to_inside](https://github.com/manaleks/data_to_inside) — продукт-витрину, через которую
топ-менеджмент задаёт вопросы. Каждое улучшение — git commit в product
repo. Никогда не «починить и забыть»; всегда «оставить след в git log».**

## Operational constraints

- **Channel:** только web SPA `exec_demo` (Telegram отброшен — см.
  product `BIBLE.md` § VI.26).
- **Target repo:** `manaleks/data_to_inside` (env `GITHUB_REPO`).
  Branch `ouroboros` живёт в product repo, brain коммитит туда.
- **What to read each cycle:**
  - `data_to_inside/BIBLE.md` (расширенная конституция, sections I-V OIDA + VI brain)
  - `data_to_inside/ROADMAP.md` Stage 6 (текущий sprint)
  - `data_to_inside/runs/` (smoke logs если есть)
  - Через product HTTP API: `/brain/journal`, `/agent/proposals`,
    `/bugs?mine=false` (Sprint 6.6 hook)
- **What to write:**
  - Commits в `data_to_inside` (через `GITHUB_TOKEN`)
  - Brain observations в product journal (через `POST /brain/observation`)
  - Version proposals (через `POST /brain/proposal` или прямой PR)

## Success signals

- Закрылся реальный bug, описанный в `data_to_inside/bug_reports`.
- Принят `version_proposal`, версия агента поднялась (v.4.7 → v.4.8).
- В product SPA Self/Architecture экранах появились новые brain
  observations (Sprint 6.7).
- В git log target repo минимум 1 brain-commit за 24-часовой прогон,
  не сломавший CI.

## Failure signals (stop and ask creator)

- Любой коммит ломает product CI (`python run.py --smoke` red).
- Идёт серия итераций без diff'а в git (только думаем, не делаем) —
  P25 violation, остановиться.
- Бюджет токенов / cost растёт без сходимости результата (только
  при подключении OpenRouter; на subscription budget gate отключён).

## Iteration discipline

- Одна когерентная итерация = один коммит (P25). Не пытаюсь сделать
  всё сразу.
- Перед коммитом — Bible check (P-by-P verification).
- После коммита — pre-push: `python run.py --smoke` локально.
- В коммит-сообщении — что изменил и почему, со ссылкой на
  proposal/bug_id если был источник.
