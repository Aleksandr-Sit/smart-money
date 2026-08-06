"""Создание ГОРЯЧЕГО кошелька для бота. Приватный ключ не показывается на экране.

Почему так: ключ, показанный в терминале, попадает в историю консоли, в скроллбек, в
скриншоты и в буфер обмена. Здесь он пишется напрямую в .env, а наружу отдаётся только
публичный адрес. Прочитать ключ можно только открыв .env самому — это осознанное действие.

ЗАЩИТА ОТ ПОТЕРИ СРЕДСТВ: если SOLANA_PRIVATE_KEY уже задан, генерация ОТКАЗЫВАЕТСЯ.
Перезапись ключа означала бы, что деньги на старом кошельке становятся недоступны боту.

Run:  .venv\\Scripts\\python.exe -m src.new_wallet [--force-new-file]
"""
from __future__ import annotations

import argparse
import os

from . import config

ENV = config.ROOT / ".env"
KEY = "SOLANA_PRIVATE_KEY"


def _existing_key_present() -> bool:
    if not ENV.exists():
        return False
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{KEY}=") and len(line.split("=", 1)[1].strip()) > 10:
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Создать горячий кошелёк бота")
    ap.add_argument("--force-new-file", action="store_true",
                    help="создать .env, если его нет")
    args = ap.parse_args()

    if _existing_key_present():
        print("ОТКАЗ: SOLANA_PRIVATE_KEY уже задан в .env")
        print("Перезапись сделала бы недоступными средства на текущем кошельке.")
        print("Если кошелёк нужно сменить — сначала выведите с него всё, потом удалите")
        print("строку из .env вручную и запустите снова.")
        return
    if not ENV.exists() and not args.force_new_file:
        print(f"ОТКАЗ: {ENV} не найден. Запустите с --force-new-file, чтобы создать.")
        return

    from solders.keypair import Keypair
    kp = Keypair()
    secret_b58 = str(kp)                      # base58 приватного ключа (формат Phantom)

    with open(ENV, "a", encoding="utf-8") as f:
        f.write(f"\n# горячий кошелёк бота (создан {os.environ.get('USERNAME', 'local')})\n")
        f.write(f"{KEY}={secret_b58}\n")
    del secret_b58                            # не держим в памяти дольше нужного

    addr = str(kp.pubkey())
    print("=" * 70)
    print("ГОРЯЧИЙ КОШЕЛЁК СОЗДАН")
    print("=" * 70)
    print(f"\nПУБЛИЧНЫЙ АДРЕС (на него пополнять):\n\n    {addr}\n")
    print("Приватный ключ записан в .env и на экран НЕ выводился.")
    print("\nЧТО СДЕЛАТЬ ДАЛЬШЕ:")
    print("  1. РЕЗЕРВНАЯ КОПИЯ: откройте .env, скопируйте значение SOLANA_PRIVATE_KEY")
    print("     и сохраните ОФЛАЙН (бумага/менеджер паролей). Без неё при потере VPS")
    print(f"     средства на кошельке ({addr[:8]}…) будут недоступны.")
    print("  2. Перенести на VPS (файл→файл, значение нигде не отображается):")
    print("     scp .env vps-trader:/opt/smart-money/.env")
    print("  3. Пополнить адрес: $400 банк + ~0.05 SOL на комиссии")
    print("\nВАЖНО: это ОДНОРАЗОВЫЙ кошелёк только для бота. Не используйте его")
    print("для личных средств и не подключайте к сайтам.")


if __name__ == "__main__":
    main()
