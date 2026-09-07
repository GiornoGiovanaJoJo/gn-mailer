"""Проекты рассылки: группировка почтовых профилей."""

from __future__ import annotations

from sqlalchemy.orm import Session

from cbmail.db.models import MailProject, SmtpProfile


def test_profile_links_to_project(db: Session, user) -> None:
    project = MailProject(name="Grand Comfort", user_id=user.id)
    db.add(project)
    db.commit()

    profile = SmtpProfile(
        title="Ящик", email_host="smtp.x.ru", email_host_user="a@x.ru",
        email_host_password="p", user_id=user.id, project_id=project.id, daily_limit=1000,
    )
    db.add(profile)
    db.commit()
    db.expire_all()

    loaded = db.get(SmtpProfile, profile.id)
    assert loaded is not None
    assert loaded.project is not None
    assert loaded.project.name == "Grand Comfort"
    assert loaded.daily_limit == 1000


def test_daily_limit_defaults_to_zero(db: Session, user) -> None:
    profile = SmtpProfile(
        title="Без лимита", email_host="smtp.x.ru", email_host_user="a@x.ru",
        email_host_password="p", user_id=user.id,
    )
    db.add(profile)
    db.commit()
    db.expire_all()

    assert db.get(SmtpProfile, profile.id).daily_limit == 0  # type: ignore[union-attr]
