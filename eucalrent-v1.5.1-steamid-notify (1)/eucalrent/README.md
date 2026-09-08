# eucalrent

Готовый бот автоматической аренды Steam-аккаунтов:

- принимает оплаченные заказы Playerok;
- по сопоставленному лоту арендует аккаунт через KOSell API;
- отправляет покупателю логин, пароль и инструкцию по Steam Guard;
- по точной команде покупателя `!код` получает свежий Steam Guard-код;
- после окончания срока вызывает завершение аренды KOSell (смена пароля или выход из сессий);
- при возврате на Playerok закрывает доступ досрочно;
- отмечает товар отправленным только после успешной выдачи данных;
- предупреждает владельца в Telegram о низком балансе, ошибках и новых заказах;
- позволяет добавлять и управлять сопоставлениями лотов через Telegram;
- автоматически подхватывает новый ID после кнопки Playerok «Продать снова».

> В Telegram добавляется уже созданный лот Playerok: бот связывает его URL/ID с игрой KOSell и сроком аренды. Саму карточку товара на Playerok нужно создать заранее.

## Что понадобится

- VPS с Ubuntu 22.04/24.04 или Debian 12/13;
- Python 3.11+;
- API-ключ KOSell из профиля;
- токен Telegram-бота от `@BotFather`;
- ваш цифровой Telegram ID;
- cookies Playerok и User-Agent того же браузера.

KOSell API: <https://kosell.store/api/docs>

## Создание Telegram-бота

1. Откройте в Telegram официального `@BotFather`.
2. Отправьте `/newbot`.
3. Укажите имя `eucalrent`.
4. Придумайте свободный username, который заканчивается на `bot`, например `eucalrent_shop_bot`.
5. Скопируйте выданный токен — он понадобится в поле `TELEGRAM_BOT_TOKEN`.
6. Узнайте свой цифровой Telegram ID через `@userinfobot` — он понадобится в поле `TELEGRAM_OWNER_ID`.

Не отправляйте токен, API-ключи и cookies посторонним. Для `eucalrent` используйте отдельного Telegram-бота: токен уже работающего бота бустов сюда не подходит.

## Безопасная проверка в PowerShell

После распаковки архива откройте PowerShell в папке загрузок и выполните:

```powershell
Set-Location "$env:USERPROFILE\Downloads"
Expand-Archive -Path ".\eucalrent-v1.2.0.zip" -DestinationPath ".\eucalrent-test" -Force
Set-Location ".\eucalrent-test\eucalrent"

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install tzdata==2026.3
.\.venv\Scripts\python.exe -m pip install -e . --no-deps

Copy-Item .env.example .env -Force
notepad .env
```

Заполните в `.env` пять обязательных полей:

```dotenv
KOSELL_API_KEY=ваш_ключ_kosell
TELEGRAM_BOT_TOKEN=токен_от_BotFather
TELEGRAM_OWNER_ID=ваш_цифровой_id
PLAYEROK_COOKIES=полная_строка_cookies
PLAYEROK_USER_AGENT=полный_User-Agent
```

Сохраните файл и выполните безопасную проверку. Она не создаёт аренду:

```powershell
.\.venv\Scripts\python.exe -m kosell_bot --check
```

Если все подключения показали `OK`, запустите Telegram-панель:

```powershell
.\.venv\Scripts\python.exe -m kosell_bot
```

Напишите новому боту `/start`. По умолчанию обработка заказов выключена. Сначала добавьте лоты и только затем включите автовыдачу кнопкой в Telegram. Остановить программу можно клавишами `Ctrl+C`.

## Быстрая установка на VPS

Распакуйте архив и выполните от root:

```bash
cd /opt
unzip eucalrent-v1.2.0.zip
cd eucalrent
chmod +x install.sh
./install.sh
```

Скрипт установит зависимости, создаст `.env` и systemd-службу. Затем заполните настройки:

```bash
nano /opt/eucalrent/.env
```

Обязательные поля:

```dotenv
KOSELL_API_KEY=ваш_ключ_kosell
TELEGRAM_BOT_TOKEN=токен_от_BotFather
TELEGRAM_OWNER_ID=ваш_цифровой_id
PLAYEROK_COOKIES=полная_строка_cookies
PLAYEROK_USER_AGENT=полный_User-Agent
```

Сохранить в nano: `Ctrl+O`, `Enter`, выйти: `Ctrl+X`.

Проверка конфигурации:

```bash
cd /opt/eucalrent
.venv/bin/python -m kosell_bot --check
```

Запуск:

```bash
systemctl enable --now eucalrent
systemctl status eucalrent --no-pager
```

Логи:

```bash
journalctl -u eucalrent -f
```

Перезапуск:

```bash
systemctl restart eucalrent
```

## Добавление лота через Telegram

1. Откройте Telegram-бота и отправьте `/start`.
2. Нажмите `➕ Добавить лот`.
3. Выберите тип: `KOSell · Аренда Steam`, `SteamSMM · Похвала CS2` или `SteamSMM · Автореги Steam`.
4. Пришлите ссылку/ID уже созданного товара Playerok.
5. Для KOSell выберите игру и срок; для похвалы укажите `Дружелюбный Учитель Лидер`; для авторегов выберите актуальную категорию и количество.
6. Проверьте себестоимость и нажмите `✅ Добавить`.

Лоты отображаются в `📦 Лоты`, где их можно включать/выключать и удалять. Для SteamSMM при добавлении авторегов каталог и цена проверяются через API непосредственно перед сохранением.

Бот сохраняет полный внутренний ID Playerok, поэтому короткая ссылка корректно сопоставляется с оплаченной сделкой.

## Как определяется перевыставленный лот

После нажатия Playerok «Продать снова» внутренний ID товара может измениться. `eucalrent` проверяет активные товары раз в 60 секунд и использует два безопасных уровня сопоставления:

1. Сначала сравнивает короткий 12-символьный ID в начале slug/ссылки, например `2332c147e2e5-...`. Это та же схема, которая использовалась в боте бустов.
2. Если Playerok создал и новый короткий ID, бот переносит привязку только когда найден ровно один старый и ровно один новый лот с одинаковым названием.

При нескольких одинаковых кандидатах бот ничего не угадывает и сообщает владельцу в Telegram. Старый ID сохраняется как псевдоним, поэтому запоздалое повторное событие заказа не создаст вторую аренду.

Настройки:

```dotenv
AUTO_RELIST_SYNC=true
RELIST_SYNC_SECONDS=60
```

## Сообщение покупателю

После выдачи покупатель получает:

```text
✅ АРЕНДА ВЫДАНА

Игра: ...
Срок: ...
Логин Steam: ...
Пароль Steam: ...
Доступ до: ...

🔐 Steam Guard
Чтобы получить код, напишите команду: !код
Если Steam покажет ошибку или попросит новый код, снова напишите: !код
```

Точная команда `!код` работает только в чате активной оплаченной аренды. Регистр не важен. После окончания аренды или возврата код не выдаётся.

## Низкий баланс

По умолчанию порог — `100 ₽`. Его можно изменить кнопкой `⚙️ Порог баланса` в Telegram или в `.env` до первого запуска:

```dotenv
LOW_BALANCE_RUB=100
```

Когда баланс опускается ниже порога, владелец получает уведомление. Повторное уведомление приходит не чаще одного раза в 6 часов, пока баланс не пополнен.

## Важные настройки `.env`

```dotenv
# Оплата аренды KOSell
RENT_CURRENCY=RUB

# Часовой пояс в сообщениях покупателю
DISPLAY_TIMEZONE=Europe/Moscow

# Необязательный Playerok-прокси: host:port или user:pass@host:port
PLAYEROK_PROXY=

# Автоматически отметить заказ Playerok отправленным после выдачи
AUTO_MARK_SENT=true

# Автоматически находить перевыставленные лоты
AUTO_RELIST_SYNC=true
RELIST_SYNC_SECONDS=60

# При первом запуске оставить обработку заказов выключенной
BOT_ENABLED_DEFAULT=false
```

На одном VPS можно одновременно держать старый бот бустов и `eucalrent`. Для них нужны разные Telegram-токены и разные лоты. Cookies и User-Agent Playerok могут быть одинаковыми. Не подключайте один и тот же лот сразу к двум ботам.

## Надёжность и безопасность

- KOSell получает уникальный ключ `playerok-<ID сделки>`, поэтому повтор события или рестарт не арендует второй аккаунт.
- Пароли Steam не сохраняются в SQLite и не пишутся в логи.
- Telegram-панель отвечает только `TELEGRAM_OWNER_ID`.
- Секреты находятся в `.env`; установщик задаёт файлу права `600`.
- Незавершённая выдача восстанавливается после перезапуска.
- При естественном завершении KOSell обычно закрывает доступ сам; бот дополнительно вызывает endpoint завершения и фиксирует результат.

## Обновление

Замените файлы проекта новой версией, не удаляя `.env` и каталог `data`, затем:

```bash
cd /opt/eucalrent
.venv/bin/pip install -r requirements.txt
systemctl restart eucalrent
```

## Диагностика

```bash
cd /opt/eucalrent
.venv/bin/python -m kosell_bot --check
```

Проверка валидирует `.env`, соединение KOSell, баланс и авторизацию Playerok. Никакая аренда при этой команде не создаётся.
