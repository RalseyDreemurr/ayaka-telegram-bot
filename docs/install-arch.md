# Нативный Arch Linux

Python, Аяка и SQLite работают в Arch. Ollama может работать локально
или на доверенном отдельном хосте. Эти команды рассчитаны на Arch x86-64.
Все 142 автотеста прошли в Arch WSL с Python 3.14. Проверены запуск через
Fish/systemd и ручной запуск в перемещённом каталоге. Нативное оборудование
и GPU требуют проверки на вашей машине.

## Подготовка

```bash
sudo pacman -Syu --needed python python-pip git curl ca-certificates tzdata sqlite
python3 --version
git clone https://github.com/RalseyDreemurr/ayaka-telegram-bot.git ~/ayaka-telegram-bot
cd ~/ayaka-telegram-bot
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m pip check
test -e .env || install -m 600 .env.example .env
```

Пакет `sqlite` даёт CLI для диагностики. Для работы Аяка пользуется
модулем `sqlite3` в Python. Для Fish-обёрток отдельно установите
`sudo pacman -S --needed fish`.

Для локального Tor при необходимости установите
`sudo pacman -S --needed tor`. Транспорт obfs4 нужен только для таких
мостов. `obfs4proxy` отсутствует в проверенных официальных репозиториях
Arch. Если используете AUR, сначала проверьте PKGBUILD и источник.
Если Tor уже предоставляется отдельно, настройте его SOCKS5-адрес.
[Пакет Tor](https://archlinux.org/packages/extra/x86_64/tor/) ·
[Установка из AUR](https://wiki.archlinux.org/title/Arch_User_Repository)

## Ollama и модель

Установите Ollama по [официальной инструкции для Linux](https://docs.ollama.com/linux).
Если у вас есть служба Ollama, запустите её штатным способом. Для ручного
запуска выполните `ollama serve` в отдельном терминале. Из второго терминала:

```bash
ollama pull qwen3:8b-q4_K_M
ollama list
curl --noproxy '*' --fail --max-time 10 http://127.0.0.1:11434/api/tags
```

Для локальной Ollama оставьте адрес из шаблона. Для внешней установите
`OLLAMA_BASE_URL` на проверенный адрес. Для Radeon см. [Vulkan](amd-vulkan.md).

## Telegram и чистая SQLite

В `.env` укажите токен BotFather, числовой `OWNER_ID`, правильные
`OLLAMA_MODEL` и `OLLAMA_BASE_URL`. Шаблон выбирает прямой Telegram.
Для Tor задайте `TELEGRAM_TRANSPORT=socks5` и `TELEGRAM_PROXY_URL`,
затем настройте [отдельный torrc](../OPERATIONS.md#необязательный-tor).

```bash
./venv/bin/python -B ayakactl.py init-db
./venv/bin/python -B ayakactl.py verify-backup data/ayaka-release.sqlite3
./venv/bin/python -B main.py --release
```

Если настроен другой `DB_PATH`, подставьте этот путь в команду проверки.
Из другой вкладки можно проверить состояние без Fish:

```bash
./venv/bin/python -B ayakactl.py status
```

Остановите ручной экземпляр через `Ctrl+C`. Для перезапуска дождитесь
завершения и запустите его снова.

## Fish и пользовательский systemd

Проверьте менеджер командой `systemctl --user show -p Version`. На системах
без пользовательского systemd используйте ручной запуск.

```bash
./start_ayaka.fish --release
./status_ayaka.fish
./restart_ayaka.fish --release
./stop_ayaka.fish
```

Fish использует venv рядом со скриптом. Для постоянного управления и
установки unit-файлов см. [руководство systemd](../OPERATIONS.md#постоянные-пользовательские-службы).
В личке владельца проверьте `/help`, `/model` и отправьте короткое сообщение.
