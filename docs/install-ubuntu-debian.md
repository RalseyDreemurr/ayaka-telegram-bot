# Нативный Ubuntu и Debian

Команды рассчитаны на Ubuntu 24.04 LTS (Python 3.12) и Debian 13 (Python 3.13)
на x86-64. Debian 12 предоставляет Python 3.11, но проверка этой инструкции
сосредоточена на Debian 13. Ubuntu 22.04 по умолчанию поставляется с Python
3.10. Для него потребуется отдельный Python 3.11+ или обновление ОС.
[Python Ubuntu 24.04](https://packages.ubuntu.com/noble/python3) ·
[Python Debian 13](https://packages.debian.org/trixie/python3-venv) ·
[Python Debian 12](https://packages.debian.org/bookworm/python3-venv)

## Подготовка

В Ubuntu 24.04 с установленным `sudo`:

```bash
sudo apt update
sudo apt install python3 python3-venv git curl ca-certificates tzdata sqlite3
```

В Debian 13 список базовых пакетов тот же. Если `sudo` не настроен,
выполните только команды apt под администратором. Клонируйте и запускайте
Аяку от своего обычного пользователя.

```bash
python3 --version
git clone https://github.com/RalseyDreemurr/ayaka-telegram-bot.git ~/ayaka-telegram-bot
cd ~/ayaka-telegram-bot
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m pip check
test -e .env || install -m 600 .env.example .env
```

`python3-venv` использует системную версию Python и создаёт отдельный pip.
Устанавливайте зависимости через интерпретатор venv. `sqlite3` как
отдельный пакет нужен для диагностики, само хранилище использует модуль Python.

## Ollama и модель

Установите Ollama по [официальному руководству для Linux](https://docs.ollama.com/linux).
Если уже есть служба Ollama, запустите её штатным способом. Для ручного
сервера используйте `ollama serve` в отдельном терминале. Для локальной
установки Ollama держите API на loopback:

```bash
ollama pull qwen3:8b-q4_K_M
ollama list
curl --noproxy '*' --fail --max-time 10 http://127.0.0.1:11434/api/tags
```

Для меньшей машины выберите подходящую модель и запишите её точный тег.
Для внешней Ollama задайте доверенный URL в `OLLAMA_BASE_URL`.
[Драйверы AMD и проверка Vulkan](amd-vulkan.md).

## Telegram и SQLite

Заполните в `.env` токен, положительный числовой `OWNER_ID`, URL Ollama
и тег модели. Прямой режим в шаблоне не требует Tor. Если нужен локальный
Tor, установите его отдельно. При мостах obfs4 установите и транспорт:

```bash
sudo apt install tor
sudo apt install obfs4proxy
```

В Ubuntu `obfs4proxy` находится в Universe. В Debian 13 — в основном
репозитории. Если стандартная служба Tor уже слушает нужный SOCKS5, используйте
её или выберите отдельный порт для экземпляра Аяки. [Tor в Debian](https://packages.debian.org/trixie/tor) ·
[obfs4proxy в Debian](https://packages.debian.org/trixie/obfs4proxy) ·
[obfs4proxy в Ubuntu](https://packages.ubuntu.com/noble/obfs4proxy)

Для режима Tor укажите `TELEGRAM_TRANSPORT=socks5`, адрес прокси и
[настройте torrc](../OPERATIONS.md#необязательный-tor). При ручном запуске
Tor должен уже работать.

```bash
./venv/bin/python -B ayakactl.py init-db
./venv/bin/python -B ayakactl.py verify-backup data/ayaka-release.sqlite3
./venv/bin/python -B main.py --release
```

При другом `DB_PATH` проверяйте этот файл. В личке владельца проверьте
`/help`, `/model` и обычное сообщение. Команда статуса из соседнего терминала:

```bash
./venv/bin/python -B ayakactl.py status
```

Остановка ручного экземпляра — `Ctrl+C`. Чтобы перезапустить, дождитесь
завершения и запустите заново. Ручной бот работает без Fish и systemd.

## Fish и systemd

Для обёрток установите Fish командой `sudo apt install fish`. Проверьте
менеджер пользователя через `systemctl --user show -p Version`.
Если он доступен, все команды выполняются из каталога проекта:

```bash
./start_ayaka.fish --release
./status_ayaka.fish
./restart_ayaka.fish --release
./stop_ayaka.fish
```

Постоянные unit-файлы устанавливаются отдельно. Для другой папки
скорректируйте копии шаблонов. [Настройка systemd и журналы](../OPERATIONS.md#постоянные-пользовательские-службы)

## Проверка совместимости

В изолированных x86-64 rootfs Ubuntu 24.04 (Python 3.12.3) и
Debian 13 (Python 3.13.5) проверены создание venv, установка зависимостей
из PyPI, `pip check`, создание и целостность пустой SQLite. Все 142 теста
прошли в каждой среде. Тесты используют заглушки Telegram и Ollama.
Эти среды работали под ядром WSL. Нативная загрузка ОС, физические GPU
и живые подключения Telegram в них не проверялись.
