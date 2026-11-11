import pytest_asyncio

from bot.db import Database


@pytest_asyncio.fixture
async def db():
    database = await Database.open(":memory:")
    yield database
    await database.close()
