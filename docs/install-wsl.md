# Windows 10/11 + WSL2 + Ollama на Windows

Аяка, SQLite и при необходимости Tor запускаются в Linux. Ollama использует
процессор или Radeon со стороны Windows. Из WSL Аяка обращается к API Ollama
по HTTP. Настройки Vulkan в этом варианте применяются в Windows.
Для текущей Windows Ollama используйте Windows 10 22H2 или новее либо
Windows 11. [Требования Ollama](https://docs.ollama.com/windows#system-requirements)

## 1. Установите WSL2 и дистрибутив

В PowerShell с правами администратора:

```powershell
wsl --install --no-distribution
wsl --update
wsl --set-default-version 2
wsl --list --online
wsl --install -d archlinux
wsl --list --verbose
```

Перезагрузите Windows, если установщик попросит. Если `archlinux` отсутствует
в каталоге, используйте официальный образ Arch WSL. Альтернатива — Ubuntu
24.04 или Debian 13. Для Arch настройте обычного Linux-пользователя по
руководству дистрибутива. В выводе `wsl --list --verbose` версия нужного
дистрибутива должна быть `2`. [Установка WSL](https://learn.microsoft.com/en-us/windows/wsl/install) ·
[Официальный Arch WSL](https://wiki.archlinux.org/title/Install_Arch_Linux_on_WSL)

## 2. Установите зависимости бота

Для Arch WSL выполните из Linux:

```bash
sudo pacman -Syu --needed python python-pip git curl ca-certificates tzdata sqlite
```

Из [инструкции Arch](install-arch.md#подготовка) возьмите блок
клонирования и создания venv. Для Ubuntu или Debian используйте
[команды apt](install-ubuntu-debian.md#подготовка). Ollama устанавливается
на следующем шаге в Windows.

Для режима с Tor установите `tor`. Если нужны мосты obfs4, установите
транспорт отдельно и настройте закрытый `torrc`. [Настройка Tor](../OPERATIONS.md#необязательный-tor)

Fish требуется только для обёрток пользовательского systemd. Для них
проверьте `systemctl --user show -p Version` после входа под обычным
пользователем. Если systemd ещё выключен в WSL, для этого режима добавьте
`systemd=true` в раздел `[boot]` файла `/etc/wsl.conf` дистрибутива.
Перезапустите WSL после завершения своих задач. Ручной запуск ниже
обходится без этих изменений. [systemd в WSL](https://learn.microsoft.com/en-us/windows/wsl/systemd/)

## 3. Запустите Ollama на Windows

Установите официальную Windows-версию Ollama и запустите её. Проверьте
из PowerShell:

```powershell
ollama pull qwen3:8b-q4_K_M
ollama list
curl.exe --fail --max-time 10 http://127.0.0.1:11434/api/tags
```

Если Windows не видит API и запущенную Ollama, сначала исправьте локальный
запуск. Для Radeon установите драйвер AMD, соответствующий вашей карте,
и проверьте доступный backend. [Ollama на Windows](https://docs.ollama.com/windows) ·
[Radeon и Vulkan](amd-vulkan.md)

## 4. Подключите WSL к Ollama

### Mirrored networking

Поддерживается в Windows 11 22H2 и новее с актуальным WSL. Чтобы выбрать
этот режим, добавьте параметр в существующий раздел `[wsl2]` файла
`%UserProfile%/.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

Чтобы WSL прочитал изменения, остановите свои задачи и выполните
`wsl --shutdown` из PowerShell. Команда завершает **все** запущенные
дистрибутивы WSL. После повторного запуска проверьте из Linux:

```bash
curl --noproxy '*' --fail --max-time 10 http://127.0.0.1:11434/api/tags
```

Если API отвечает, используйте в `.env`
`OLLAMA_BASE_URL=http://127.0.0.1:11434`. Ollama в Windows может
оставаться привязанной к loopback. Если запрос не проходит, проверьте
режим WSL, состояние Windows Ollama и правила брандмауэра. [Сетевые режимы WSL](https://learn.microsoft.com/en-us/windows/wsl/networking#mirrored-mode-networking)

### Стандартная NAT-сеть

Из WSL адрес `127.0.0.1` обычно относится к Linux. В Bash внутри WSL
узнайте адрес Windows на маршруте WSL и исходный адрес Linux:

```bash
windows_wsl_ip=$(ip -4 route show default | awk 'NR == 1 { print $3 }')
ip -4 route get "$windows_wsl_ip"
```

Нужен IPv4 Windows на виртуальном интерфейсе WSL. В выводе второй
команды `src` — IPv4 Linux со стороны этого соединения. Запомните оба
адреса. VPN может менять маршрут. В Windows сверяйте адрес виртуального
интерфейса по `Get-NetIPAddress -AddressFamily IPv4`.

1. Выйдите из Windows Ollama через значок в трее.
2. В пользовательских переменных среды Windows задайте
   `OLLAMA_HOST=WINDOWS_WSL_IP:11434`. Замените `WINDOWS_WSL_IP` фактическим
   адресом Windows со стороны WSL. Ограничьте привязку этим интерфейсом.
3. Если требуется входящее правило брандмауэра Windows, ограничьте его
   TCP-портом `11434`, локальным адресом виртуального интерфейса Windows
   и удалённым адресом WSL из предыдущего шага. Сохраните брандмауэр
   включённым. Избегайте правил для всех источников, WAN и проброса порта.
4. Снова запустите Ollama из меню «Пуск». Проверьте адрес прослушивания:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 11434 | Select-Object LocalAddress, LocalPort
```

Затем проверьте из WSL:

```bash
curl --noproxy '*' --fail --max-time 10 "http://$windows_wsl_ip:11434/api/tags"
```

Если запрос успешен, укажите `OLLAMA_BASE_URL=http://WINDOWS_WSL_IP:11434`
в `.env` с фактическим адресом. Проверьте, что API недоступен с других машин.
После перезапуска WSL адрес интерфейса может измениться. Повторно сверяйте
`OLLAMA_HOST`, правило и `OLLAMA_BASE_URL`. [Модель NAT в WSL](https://learn.microsoft.com/en-us/windows/wsl/networking#default-networking-mode-nat) ·
[Переменные Ollama в Windows](https://docs.ollama.com/faq#setting-environment-variables-on-windows)

## 5. Настройте Telegram и запустите Аяку

В каталоге проекта создайте `.env` из шаблона. Запишите токен, числовой
`OWNER_ID`, проверенный `OLLAMA_BASE_URL` и точный `OLLAMA_MODEL`. Для
исходной архитектуры с Tor укажите:

```dotenv
TELEGRAM_TRANSPORT=socks5
TELEGRAM_PROXY_URL=socks5://127.0.0.1:9050
```

Здесь loopback относится к Tor внутри WSL. Если Telegram доступен напрямую,
можно выбрать `direct` и оставить `TELEGRAM_PROXY_URL` пустым.

```bash
./venv/bin/python -B ayakactl.py init-db
./venv/bin/python -B main.py --release
```

В режиме SOCKS5 ручной запуск предполагает, что Tor уже работает.
Остановка ручного экземпляра — `Ctrl+C`. Для управления службой используйте
`./start_ayaka.fish --release`, `./status_ayaka.fish` и `./stop_ayaka.fish`.
Для проверки в личке владельца используйте `/help` и `/model`.
[Включение systemd в WSL](https://learn.microsoft.com/en-us/windows/wsl/systemd/)

## Диагностика сети

| Наблюдение | Что проверить |
| --- | --- |
| API не отвечает из самой Windows | Запущена ли Ollama, какой адрес она слушает, загружена ли модель. |
| Работает из Windows, из WSL приходит `Connection refused` | Адрес и порт Ollama. При NAT проверьте привязку к виртуальному интерфейсу Windows. |
| Из WSL истекает таймаут | Маршрут, VPN, параметры Windows Firewall и Hyper-V Firewall, адрес текущей WSL VM. |
| `/api/tags` отвечает, модели в списке нет | Точный тег из `ollama list` и завершение загрузки модели. |
| Telegram не отвечает, а модель доступна | Проверяйте Bot API и Tor отдельно. Сеть Ollama не зависит от Tor. |
