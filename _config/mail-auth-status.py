"""Проверка SPF, DKIM и DMARC у доменов, с которых уходит почта.

Зачем. С февраля 2024 Gmail требует от массовых отправителей все три записи.
Нет хотя бы одной — письма уходят в спам, а в спаме почта не загружает
картинки. Снаружи это выглядит как «шаблон сломан, картинки не доезжают»,
и чинят вёрстку, хотя дело в DNS.

Проверять руками неудобно и легко забыть: у каждого клиента свой домен, и
добавляется он тогда, когда про DNS уже никто не помнит. Скрипт обходит все
домены, с которых сервис реально отправляет, и говорит, чего не хватает и
какую именно запись добавить.

Запросы идут через DNS-over-HTTPS (dns.google): в контейнере обычно нет ни
dig, ни nslookup, а доступ в интернет есть.

Запуск на сервере (файл кладёт деплой, внутрь образа он не попадает):
    docker cp _config/mail-auth-status.py cbmail-web:/tmp/mail-auth-status.py
    docker exec cbmail-web python /tmp/mail-auth-status.py

Можно проверить и чужие домены, не заглядывая в базу:
    python mail-auth-status.py grandcomfort.ru zoomlogistic.ru

Код возврата всегда 0: это отчёт, а не проверка.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

DOH_URL = "https://dns.google/resolve"
TIMEOUT_SECONDS = 15

#: Селекторы DKIM, которые ставят распространённые почтовые провайдеры.
#: Список нужен потому, что селектор нигде не записан: его выбирает провайдер,
#: и узнать его можно либо из заголовков письма, либо перебором.
DKIM_SELECTORS = (
    "mail",
    "mailru",
    "dkim",
    "default",
    "selector1",
    "selector2",
    "google",
    "yandex",
    "mx",
    "sw",
    "spaceweb",
    "key1",
)


def _txt(name: str) -> list[str]:
    """TXT-записи имени. Пустой список — записи нет или DNS не ответил."""
    query = urllib.parse.urlencode({"name": name, "type": "TXT"})
    request = urllib.request.Request(f"{DOH_URL}?{query}", headers={"Accept": "application/dns-json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — для отчёта «нет записи» причина неважна
        return []
    out: list[str] = []
    for answer in data.get("Answer") or []:
        value = (answer.get("data") or "").strip()
        # DNS отдаёт длинные записи кусками в кавычках — склеиваем обратно.
        out.append("".join(part for part in value.split('" "')).strip('"'))
    return out


def find_spf(domain: str) -> list[str]:
    """Все записи SPF домена.

    Именно все, а не первая попавшаяся: двух записей SPF быть не должно, и
    это не придирка. По RFC 7208 вторая запись — не «возьмём какую-нибудь»,
    а ошибка проверки: SPF у такого домена не проходит вообще, хотя обе
    записи по отдельности выглядят правильными. Поймать это можно только
    посмотрев на список целиком.
    """
    return [record for record in _txt(domain) if record.lower().startswith("v=spf1")]


def has_mx(domain: str) -> bool:
    """Принимает ли домен почту — по наличию MX.

    Отличать нужно потому, что совет разный. Домену, с которого шлют письма,
    нужен мягкий DMARC в режиме наблюдения. Домену, который почтой не
    пользуется вовсе, наоборот нужен жёсткий запрет: иначе от его имени
    может писать кто угодно, и репутацию испортят за него.
    """
    query = urllib.parse.urlencode({"name": domain, "type": "MX"})
    request = urllib.request.Request(f"{DOH_URL}?{query}", headers={"Accept": "application/dns-json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — для отчёта «нет записи» причина неважна
        return True  # не смогли проверить — считаем почтовым, совет будет мягче
    return bool(data.get("Answer"))


def find_dmarc(domain: str) -> str | None:
    for record in _txt(f"_dmarc.{domain}"):
        if record.lower().startswith("v=dmarc1"):
            return record
    return None


def find_dkim(domain: str) -> str | None:
    """Селектор DKIM, если нашёлся перебором известных."""
    for selector in DKIM_SELECTORS:
        for record in _txt(f"{selector}._domainkey.{domain}"):
            if "p=" in record:
                return selector
    return None


def suggested_dmarc(domain: str, sends_mail: bool) -> str:
    """Запись, которую нужно добавить.

    Для домена, с которого шлют письма, — режим наблюдения: он закрывает
    требование Gmail и при этом ничего не блокирует, то есть испортить
    доставку им нельзя. Ужесточать до quarantine и reject имеет смысл
    потом, когда по отчётам видно, что всё подписывается правильно.
    """
    if not sends_mail:
        # Без rua: ящика на домене без почты не существует, а отчёты на чужой
        # домен требуют отдельного разрешения на стороне получателя — для
        # запаркованного домена эта возня не окупается.
        return "v=DMARC1; p=reject"
    return f"v=DMARC1; p=none; rua=mailto:dmarc@{domain}; fo=1"


def report(domain: str) -> bool:
    """Печатает состояние домена. Возвращает True, если всё на месте."""
    print(f"=== {domain} ===")
    spf_records = find_spf(domain)
    dkim = find_dkim(domain)
    dmarc = find_dmarc(domain)

    if not spf_records:
        print("  SPF:   НЕТ")
    else:
        for record in spf_records:
            print(f"  SPF:   {record}")
    print(f"  DKIM:  {'есть, селектор ' + dkim if dkim else 'не найден по известным селекторам'}")
    print(f"  DMARC: {dmarc if dmarc else 'НЕТ'}")

    # Домен без MX и без SPF почтой не пользуется — это сайт, а не отправитель.
    # Требовать от него DKIM бессмысленно, а защита нужна другая.
    sends_mail = has_mx(domain) or bool(spf_records)
    if not sends_mail:
        print("  Почту этот домен не отправляет и не принимает (нет MX).")
        print("  Но защитить его стоит: без записей от его имени может писать кто угодно,")
        print("  и портить будут вашу репутацию. Добавьте две записи TXT:")
        print(f"      имя: {domain}          значение: v=spf1 -all")
        print(f"      имя: _dmarc.{domain}   значение: {suggested_dmarc(domain, sends_mail=False)}")
        return False

    problems: list[str] = []
    if not spf_records:
        problems.append("SPF")
    elif len(spf_records) > 1:
        problems.append("SPF (их несколько)")
    if not dkim:
        problems.append("DKIM")
    if not dmarc:
        problems.append("DMARC")

    if not problems:
        if dmarc and "rua=" not in dmarc.lower():
            print("  Всё на месте, но в DMARC нет rua= — отчёты о доставке никуда не приходят,")
            print(f"  и увидеть, что что-то сломалось, будет неоткуда. Добавьте: rua=mailto:dmarc@{domain}")
            return True
        print("  Всё на месте.")
        return True

    print(f"  НЕ ХВАТАЕТ: {', '.join(problems)} -> письма будут попадать в спам")
    if len(spf_records) > 1:
        print(f"  Записей SPF {len(spf_records)}, а должна быть одна. Это не придирка:")
        print("  при двух записях проверка SPF завершается ошибкой, и письма не проходят")
        print("  её вообще — хотя каждая запись по отдельности выглядит правильной.")
        print("  Сведите их в одну, сохранив всех отправителей. Из разных записей")
        print("  механизмы просто переносятся в общую, а redirect= становится include:.")
    if not dmarc:
        print("  Добавьте запись TXT:")
        print(f"      имя:      _dmarc.{domain}")
        print(f"      значение: {suggested_dmarc(domain, sends_mail=True)}")
        print("  p=none ничего не блокирует — это режим наблюдения, доставку им не испортить.")
        print(f"  Ящик dmarc@{domain} должен существовать, иначе отчёты будут отбиваться.")
    if not dkim:
        print("  DKIM включается в панели почтового провайдера (у Mail.ru, Яндекса и")
        print("  хостингов это делается одной галочкой, запись он выдаёт сам).")
    if not spf_records:
        print(f"  SPF — запись TXT на самом {domain}, перечисляет, кому можно слать от вашего имени.")
    return False


def domains_from_db() -> list[str]:
    """Домены отправителей из настроек почты сервиса."""
    try:
        from cbmail.db.models import SmtpProfile
        from cbmail.db.session import get_sync_engine
        from sqlalchemy import select
        from sqlalchemy.orm import Session as OrmSession
    except Exception as exc:  # noqa: BLE001 — сообщение важнее разбора причины
        print(f"Не удалось прочитать почтовые профили: {exc}")
        return []

    found: set[str] = set()
    with OrmSession(get_sync_engine()) as session:
        for profile in session.execute(select(SmtpProfile)).scalars():
            for address in (profile.default_from_email, profile.email_host_user):
                if address and "@" in address:
                    found.add(address.rsplit("@", 1)[1].strip().lower())
    return sorted(found)


def main() -> int:
    domains = [a.strip().lower() for a in sys.argv[1:] if not a.startswith("-")]
    if domains:
        print("Проверяю домены, переданные в командной строке.\n")
    else:
        domains = domains_from_db()
        if not domains:
            print("Почтовых профилей нет — проверять нечего.")
            print("Можно указать домены вручную: python mail-auth-status.py example.ru")
            return 0
        print(f"Домены отправителей из настроек сервиса: {', '.join(domains)}\n")

    bad = 0
    for domain in domains:
        if not report(domain):
            bad += 1
        print()

    if bad:
        print(f"Доменов с незакрытыми требованиями: {bad} из {len(domains)}.")
        print("Пока записи не добавлены, письма будут уходить в спам, а в спаме")
        print("почта не загружает картинки — выглядит это как сломанный шаблон.")
    else:
        print("Все домены настроены. Записи в DNS обновляются не мгновенно:")
        print("если что-то добавили только что, перепроверьте через час.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
