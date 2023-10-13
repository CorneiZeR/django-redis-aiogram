# django-redis-aiogram

`django-redis-aiogram` provides a quick way to install `aiogram` in a container adjacent to `django`, allowing you to use your own router and loop. Also Allows you to send messages through `redis`.

### Supported Python and Django releases

Current release of `django-redis-aiogram` is **1.0.4**, and it supports Python 3.8+ and Django 4.2+.

## Installation

The easiest and recommended way to install `django-redis-aiogram` is from [PyPI](https://pypi.org/project/django-redis-aiogram/)

``` shell
pip install django-redis-aiogram
```

You need to add `telegram_bot` to `INSTALLED_APPS` in your projects `settings.py`.

``` python
# settings.py

INSTALLED_APPS = (
    ...
    'telegram_bot',
    ...
)
```

Also, you need to specify the minimum settings:
``` python
# settings.py

TELEGRAM_BOT = {
    'REDIS_URL': REDIS_URL,
    'TOKEN': TELEGRAM_BOT_TOKEN
}
```

Next, add a separate container to your docker-compose.yml.

``` yaml
# docker-compose.yml

services:
  ...
  
  telegram_bot:
    container_name: telegram_bot
    restart: always
    command: python manage.py start_tgbot
    build:
      context: ./
```

## Example Usage

To send a message, use the following code:
``` python
from aiogram import types, F
from telegram_bot import bot

bot.send_message_via_redis(chat_id=CHAT_ID, text=TEXT)
bot.send_message_via_redis(chat_id=CHAT_ID, caption=TEXT, photo=URL)

markup = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(
        text='best project ever',
        web_app=types.WebAppInfo(url='https://pypi.org/project/django-redis-aiogram')
    )]
])
bot.send_message_via_redis(chat_id=CHAT_ID, text=TEXT, reply_markup=markup)
```

If you need to use handlers, create file `tg_router.py` (by default) in your app, use the following code:

``` python
from aiogram import types, F
from telegram_bot import bot


@bot.message(F.text.startswith('/start'))
async def start_handler(message: types.Message) -> None:
    await message.answer('hi')


@bot.message()
async def simple_handler(message: types.Message) -> None:
    await message.reply(message.text)
```

YOu can use all handler types like in aiogram.

## Settings

You can override settings:

``` python
# settings.py

TELEGRAM_BOT = {
    {
    # event expiration time in redis
    'REDIS_EXP_TIME': 5,
    # redis key for handling expired event
    'REDIS_EXP_KEY': 'TELEGRAM_BOT_EXP',
    # redis key for collecting messages
    'REDIS_MESSAGES_KEY': 'TELEGRAM_BOT_MESSAGE',
    # name of the module to find
    'MODULE_NAME': 'tg_router',
    # telegram bot token
    'TOKEN': TELEGRAM_BOT_TOKEN,
    # url for redis connection
    'REDIS_URL': REDIS_URL
}
```
