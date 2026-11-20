# AMD Radeon: ускорение Ollama через Vulkan

ROCm — вычислительный стек AMD. Совместимость зависит от модели Radeon,
ОС, версии ROCm и драйвера. Ollama отдельно публикует список устройств и
LLVM-целей. Vulkan — другой backend. Он подходит, когда карта не входит
в список ROCm, но поддерживается Vulkan-драйвером. Например, на Linux
проверять можно Radeon на RDNA из серий RX 5000 и RX 6000 с RADV.
На Windows для некоторых RX 6000 Ollama отдельно рекомендует попробовать
Vulkan. Поддержка конкретной карты зависит от драйвера и сборки, а объём
VRAM влияет на размещение модели. [Ollama GPU](https://docs.ollama.com/gpu) ·
[Ollama на Windows](https://docs.ollama.com/windows) ·
[Драйвер Mesa RADV](https://docs.mesa3d.org/drivers/radv.html)

## Linux

Arch Linux:

```bash
sudo pacman -Syu --needed mesa vulkan-radeon vulkan-icd-loader vulkan-tools
vulkaninfo --summary
```

Ubuntu 24.04 и Debian 13:

```bash
sudo apt update
sudo apt install mesa-vulkan-drivers libvulkan1 vulkan-tools
vulkaninfo --summary
```

В Ubuntu 24.04 пакет `vulkan-tools` находится в Universe. Ищите физическую
Radeon в списке устройств. `llvmpipe` и `lavapipe` указывают на программный
рендеринг. Если Radeon не видна, проверьте актуальность kernel/Mesa/firmware
и доступ запустившего Ollama пользователя к `/dev/dri/renderD*`.
Для системной службы это может быть пользователь `ollama` и группа `render`.
Проверка устройства не требует запуска Ollama от root.
[Пакеты Arch RADV](https://archlinux.org/packages/extra/x86_64/vulkan-radeon/) ·
[Загрузчик Vulkan в Arch](https://archlinux.org/packages/extra/x86_64/vulkan-icd-loader/) ·
[Драйверы Ubuntu](https://packages.ubuntu.com/noble/mesa-vulkan-drivers) ·
[Драйверы Debian](https://packages.debian.org/trixie/mesa-vulkan-drivers)

## Windows

Установите соответствующий вашей карте официальный AMD Radeon Driver с
поддержкой Vulkan, затем запустите нативную Ollama. Если вы используете
WSL только для Аяки, Vulkan-драйвер в WSL не решает задачи Windows Ollama.
При наличии `vulkaninfo` из Vulkan SDK выполните `vulkaninfo --summary`
в Windows. Для фактической нагрузки подтвердите backend журналом Ollama
и размещение модели через `ollama ps`.
[Драйверы AMD](https://www.amd.com/en/support/download/drivers.html) ·
[Поддержка GPU в Ollama для Windows](https://docs.ollama.com/windows)

## Версии и выбор устройства

1. Сверьте версию сервера с документацией. Сейчас Ollama включает Vulkan
   автоматически, когда backend доступен. В версии `0.12.11` Vulkan был
   экспериментальным и требовал `OLLAMA_VULKAN=1`. Эта команда отражает
   режим конкретного старого выпуска.
2. При нескольких GPU найдите индекс в журнале запуска Ollama. Индексы
   из других утилит могут иметь иной порядок. Например, если физическая
   Radeon указана как `Vulkan1`, задайте
   `GGML_VK_VISIBLE_DEVICES=1`. Для нескольких допустимых устройств можно
   указать список индексов через запятую.
3. Задавайте переменную на стороне **сервера Ollama**. При ручном запуске
   Linux можно использовать `env GGML_VK_VISIBLE_DEVICES=1 ollama serve`.
   Для службы Linux задайте `Environment="GGML_VK_VISIBLE_DEVICES=1"` в
   systemd drop-in через `sudo systemctl edit ollama.service` и перезапустите
   службу. В Windows задайте пользовательскую переменную среды, выйдите
   из Ollama через трей и снова запустите приложение. Выбор Vulkan-индексов
   влияет на Vulkan. Если сборка выбирает другой backend, он имеет свои
   параметры.

[Поддержка GPU Ollama](https://docs.ollama.com/gpu) ·
[Примечания к Ollama 0.12.11](https://github.com/ollama/ollama/releases/tag/v0.12.11) ·
[Переменные Ollama](https://docs.ollama.com/faq#how-do-i-configure-ollama-server)

## Проверка и возврат к CPU

Загрузите модель, сделайте пробную генерацию и сразу вызовите `ollama ps`:

```bash
ollama run qwen3:8b-q4_K_M "Ответь одним словом: готова?"
ollama ps
```

`PROCESSOR` показывает загрузку в GPU, CPU или в оба. Пустой список
означает, что модель сейчас не загружена. Чтобы отличить Vulkan от ROCm,
читайте журнал. Linux-служба пишет в `journalctl -u ollama --no-pager -n 80`,
`ollama serve` выводит диагностику в консоль. В Windows журнал сервера
лежит в `%LOCALAPPDATA%\Ollama\server.log`. При необходимости включите
`OLLAMA_DEBUG=1` на время диагностики и перезапустите сервер.
[Ollama FAQ](https://docs.ollama.com/faq#how-can-i-tell-if-my-model-was-loaded-onto-the-gpu) ·
[Журналы Ollama](https://docs.ollama.com/troubleshooting)

Для отключения Vulkan текущая документация предлагает `OLLAMA_VULKAN=0`
или `GGML_VK_VISIBLE_DEVICES=-1`. Если включён ROCm, отдельно исключите его
устройства через `ROCR_VISIBLE_DEVICES=-1`. На машине с AMD и обоими
backend пример ручного запуска Linux выглядит так:

```bash
env OLLAMA_VULKAN=0 GGML_VK_VISIBLE_DEVICES=-1 ROCR_VISIBLE_DEVICES=-1 ollama serve
```

Закройте прежний экземпляр сервера перед ручным запуском. В Windows
и systemd задайте эти же параметры там, где запускается сервер, и
перезапустите его. Проверьте `100% CPU` в `ollama ps`. Если сборка выбирает
иной GPU-backend, сверяйте её журнал и параметры соответствующего backend.
Путь восстановления зависит от версии Ollama и драйвера.
[Выбор и отключение AMD GPU](https://docs.ollama.com/gpu) ·
[Установка Ollama на Linux](https://docs.ollama.com/linux)
