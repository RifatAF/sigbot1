

def test_fix_text_repairs_only_broken_text():
    from sigbot.updater import fix_text, short
    good = "Листинги Bybit (монеты, которых нет на Binance): источник, бэктест считается"
    bad = "✅ " + good.encode().decode("cp1251", errors="replace")[:120]
    assert fix_text(bad).startswith("✅ Листинги Bybit (монеты")
    for ok in ("✅ Бот запущен на компьютере pc", "Ставлю обновление в 10:00, всё в порядке"):
        assert fix_text(ok) == ok
    assert short(good) == "Листинги Bybit (монеты, которых нет на Binance)"
