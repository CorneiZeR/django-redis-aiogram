"""Handlers picked up by autodiscover during django.setup()."""

from aiogram import types

from django_redis_aiogram import bot

IMPORTED = True


@bot.message()
async def autodiscovered_message(message: types.Message) -> None:  # pragma: no cover
    ...


@bot.callback_query()
async def autodiscovered_callback(query: types.CallbackQuery) -> None:  # pragma: no cover
    ...
