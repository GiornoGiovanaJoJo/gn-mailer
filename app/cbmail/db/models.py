"""ORM-модели поверх СУЩЕСТВУЮЩЕЙ схемы базы.

Имена таблиц и колонок заданы явно и совпадают с тем, что когда-то создали
Django-миграции (``<app_label>_<model>``, FK — ``<field>_id``). Это осознанное
решение: боевые данные остаются на месте, переход не требует ни выгрузки, ни
конвертации, а откат сводится к запуску прежнего контейнера.

Переименование таблиц в «красивые» — отдельная задача, которую имеет смысл
делать уже после того, как новое приложение отработает в проде: она даёт
косметический выигрыш ценой миграции всех боевых данных.

Единственная новая таблица — ``cbmail_session`` (см. :class:`Session`).

Менять схему здесь нельзя без Alembic-миграции: модели описывают то, что уже
есть в БД, а не желаемое состояние.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _utcnow() -> datetime:
    """Python-сторонний default для меток времени.

    Столбцы времени созданы Django-миграциями через ``auto_now_add``/``auto_now``,
    которые заполняют значение В КОДЕ, а не DB-default'ом. Поэтому в боевой БД у
    этих колонок НЕТ server default: если полагаться только на ``server_default``,
    SQLAlchemy опускает колонку в INSERT и Postgres валит ``NOT NULL``. Python-side
    ``default`` включает колонку в INSERT со значением и чинит это на проде.
    (На SQLite в тестах таблицы создаются с ``server_default``, поэтому там ошибка
    не воспроизводилась — классический prod-only баг.)
    """
    return datetime.now(timezone.utc)

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Типы, одинаково работающие в PostgreSQL (прод) и SQLite (тесты).
#
# Тесты не должны требовать поднятого Postgres: это делает их медленными и
# заставляет пропускать их локально — то есть ровно тогда, когда они нужнее
# всего. В PostgreSQL оба типа отображаются в те же native-типы, что создал
# Django, поэтому схема прода не меняется.
UUID_TYPE = Uuid(as_uuid=True)
BIG_INT = BigInteger().with_variant(Integer, "sqlite")


# --- Промежуточные таблицы M2M ---------------------------------------------
# Django создаёт их без модели, поэтому описываем как Table. Имена колонок —
# `<lowercased_model>_id`, для пользователя это `profile_id` (AUTH_USER_MODEL =
# useraccount.Profile), а не `user_id`.

message_read_by = Table(
    "mail_message_read_by",
    Base.metadata,
    Column("id", BIG_INT, primary_key=True, autoincrement=True),
    Column("message_id", UUID_TYPE, ForeignKey("mail_message.id", ondelete="CASCADE")),
    Column("profile_id", UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE")),
    UniqueConstraint("message_id", "profile_id", name="mail_message_read_by_message_id_profile_id_uniq"),
)

message_starred_by = Table(
    "mail_message_starred_by",
    Base.metadata,
    Column("id", BIG_INT, primary_key=True, autoincrement=True),
    Column("message_id", UUID_TYPE, ForeignKey("mail_message.id", ondelete="CASCADE")),
    Column("profile_id", UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE")),
    UniqueConstraint("message_id", "profile_id", name="mail_message_starred_by_message_id_profile_id_uniq"),
)

message_dirs = Table(
    "mail_message_dirs",
    Base.metadata,
    Column("id", BIG_INT, primary_key=True, autoincrement=True),
    Column("message_id", UUID_TYPE, ForeignKey("mail_message.id", ondelete="CASCADE")),
    Column("messagedir_id", BIG_INT, ForeignKey("mail_messagedir.id", ondelete="CASCADE")),
    UniqueConstraint("message_id", "messagedir_id", name="mail_message_dirs_message_id_messagedir_id_uniq"),
)

message_masks = Table(
    "mail_message_masks",
    Base.metadata,
    Column("id", BIG_INT, primary_key=True, autoincrement=True),
    Column("message_id", UUID_TYPE, ForeignKey("mail_message.id", ondelete="CASCADE")),
    Column("messagemask_id", BIG_INT, ForeignKey("mail_messagemask.id", ondelete="CASCADE")),
    UniqueConstraint("message_id", "messagemask_id", name="mail_message_masks_message_id_messagemask_id_uniq"),
)

message_directory_user = Table(
    "mail_messagedirectory_user",
    Base.metadata,
    Column("id", BIG_INT, primary_key=True, autoincrement=True),
    Column("messagedirectory_id", BIG_INT, ForeignKey("mail_messagedirectory.id", ondelete="CASCADE")),
    Column("profile_id", UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE")),
    UniqueConstraint(
        "messagedirectory_id", "profile_id", name="mail_messagedirectory_user_uniq"
    ),
)


# --- Пользователь -----------------------------------------------------------


class Profile(Base):
    """Пользователь системы (Django ``useraccount.Profile``, наследник AbstractUser).

    Пароли остаются в формате Django (``pbkdf2_sha256$…``) и проверяются
    совместимой реализацией в :mod:`cbmail.core.security`, поэтому переход не
    требует сброса паролей.
    """

    __tablename__ = "useraccount_profile"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    password: Mapped[str] = mapped_column(String(128))
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str] = mapped_column(String(150), unique=True)
    first_name: Mapped[str] = mapped_column(String(150), default="")
    last_name: Mapped[str] = mapped_column(String(150), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    date_joined: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())

    type: Mapped[int] = mapped_column(SmallInteger, default=1)
    phone: Mapped[str | None] = mapped_column(String(500))
    avatar: Mapped[str] = mapped_column(String(100), default="default/user-nophoto.png")
    gender: Mapped[int] = mapped_column(SmallInteger, default=1)
    birthday: Mapped[datetime | None] = mapped_column(Date)
    city: Mapped[str | None] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    smtp_profiles: Mapped[list[SmtpProfile]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def display_name(self) -> str:
        return self.full_name or self.username

    @property
    def can_login(self) -> bool:
        """Заблокированный или удалённый пользователь не должен входить.

        Django-версия проверяла только ``is_active``, а поля ``blocked`` и
        ``deleted`` заполнялись модерацией и ни на что не влияли — снятый с
        доступа сотрудник продолжал пользоваться почтой.
        """
        return self.is_active and not self.blocked and not self.deleted


# --- Настройки SMTP ---------------------------------------------------------


class SmtpProfile(Base):
    """Профиль SMTP пользователя (``mail_usersettingssmtp``).

    Один пользователь может держать несколько профилей — по одному на
    проект/домен — и выбирать нужный при создании кампании.
    """

    __tablename__ = "mail_usersettingssmtp"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    message_header: Mapped[str | None] = mapped_column(Text)
    message_footer: Mapped[str | None] = mapped_column(Text)
    email_host: Mapped[str | None] = mapped_column(Text)
    default_from_email: Mapped[str | None] = mapped_column(Text)
    email_port: Mapped[str | None] = mapped_column(Text)
    email_host_user: Mapped[str | None] = mapped_column(Text)
    email_host_password: Mapped[str | None] = mapped_column(Text)
    email_use_tls: Mapped[bool | None] = mapped_column(Boolean, default=False)
    email_use_ssl: Mapped[bool | None] = mapped_column(Boolean, default=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE")
    )
    last_check_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_success: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    check_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[Profile] = relationship(back_populates="smtp_profiles")

    @property
    def label(self) -> str:
        name = self.title or self.email_host or "SMTP"
        sender = self.default_from_email or self.email_host_user or "—"
        return f"{name} ({sender})"

    @property
    def is_configured(self) -> bool:
        return bool(self.email_host and self.email_host_user)


class SmtpCheckLog(Base):
    __tablename__ = "mail_smtpchecklog"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    smtp_settings_id: Mapped[int] = mapped_column(
        BIG_INT, ForeignKey("mail_usersettingssmtp.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


# --- Стоп-лист --------------------------------------------------------------


class StopListRow(Base):
    """Глобальный стоп-лист (``mail_stoplist``).

    Django хранил все адреса одной строкой в ``list`` через запятую. Схему не
    меняем, но вся работа с этим полем инкапсулирована в сервисе: разбор,
    нормализация и запись идут через домен, а не через ``', '.join`` по месту.
    """

    __tablename__ = "mail_stoplist"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    list: Mapped[str] = mapped_column(Text, default="")


class Unsubscribe(Base):
    """Отписки, привязанные к отправителю (``cbmail_unsubscribe``).

    Новая аддитивная таблица (как ``cbmail_session``): создаётся идемпотентно,
    Django-схему не трогает. Появилась, потому что глобальный ``mail_stoplist``
    блокировал адрес сразу у всех отправителей: отписавшись от писем одного
    домена, человек переставал получать письма и от совсем другого.

    ``scope`` — домен отправителя (часть after ``@`` в адресе «От кого»),
    приведённый к нижнему регистру. Пустой ``scope`` означает глобальную отписку
    (старые письма со ссылкой без параметра ``d`` — обратная совместимость).
    Отписка действует только на письма с совпадающим доменом отправителя.
    """

    __tablename__ = "cbmail_unsubscribe"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    scope: Mapped[str] = mapped_column(String(255), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("email", "scope", name="cbmail_unsubscribe_email_scope_uniq"),
    )


# --- Шаблоны письма ---------------------------------------------------------


class MessageTemplate(Base):
    __tablename__ = "mail_messagetemplates"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(255), default="")
    mask: Mapped[str] = mapped_column(Text, default="")
    mask_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=func.now()
    )


# --- Кампании ---------------------------------------------------------------


class Campaign(Base):
    __tablename__ = "mail_massmailcampaign"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)

    recipient_emails: Mapped[str] = mapped_column(Text, default="")
    stop_list: Mapped[str] = mapped_column(Text, default="")
    recipient_file: Mapped[str | None] = mapped_column(String(100))

    message_count: Mapped[int] = mapped_column(Integer, default=50)
    message_interval: Mapped[int] = mapped_column(Integer, default=2)

    use_smtp_settings_id: Mapped[int | None] = mapped_column(
        BIG_INT, ForeignKey("mail_usersettingssmtp.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), default="draft")

    from_email: Mapped[str | None] = mapped_column(String(254))
    from_name: Mapped[str | None] = mapped_column(String(255))

    scheduled_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    total_recipients: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    opened_count: Mapped[int] = mapped_column(Integer, default=0)
    clicked_count: Mapped[int] = mapped_column(Integer, default=0)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[Profile] = relationship(lazy="joined")
    smtp: Mapped[SmtpProfile | None] = relationship(lazy="joined")
    attachments: Mapped[list[CampaignFile]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def progress_percent(self) -> float:
        if not self.total_recipients:
            return 0.0
        processed = self.sent_count + self.failed_count
        return min(100.0, round(processed / self.total_recipients * 100, 1))


class CampaignFile(Base):
    """Вложение, уходящее с каждым письмом кампании."""

    __tablename__ = "mail_massmailcampaignfile"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        BIG_INT, ForeignKey("mail_massmailcampaign.id", ondelete="CASCADE")
    )
    file: Mapped[str] = mapped_column(String(100))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())

    campaign: Mapped[Campaign] = relationship(back_populates="attachments")

    @property
    def filename(self) -> str:
        return self.file.rsplit("/", 1)[-1]


class CampaignLog(Base):
    """Лог отправки по каждому адресу (``mail_massmaillog``).

    Служит источником правды для «кому уже отправлено»: при возобновлении
    кампании повторная отправка исключается именно по этим записям.
    """

    __tablename__ = "mail_massmaillog"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        BIG_INT, ForeignKey("mail_massmailcampaign.id", ondelete="CASCADE")
    )
    email: Mapped[str] = mapped_column(String(254))
    status: Mapped[str] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())


# --- Сообщения --------------------------------------------------------------


class MessageDir(Base):
    __tablename__ = "mail_messagedir"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE")
    )

    __table_args__ = (UniqueConstraint("name", "user_id", name="mail_messagedir_name_user_id_uniq"),)


class MessageMask(Base):
    __tablename__ = "mail_messagemask"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE")
    )
    mask: Mapped[str] = mapped_column(Text)


class MessageRm(Base):
    """Отметка об удалении сообщения (корзина)."""

    __tablename__ = "mail_messagerm"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    key: Mapped[str] = mapped_column(String(255))


class MessageDirectory(Base):
    """Нумерованная директория, задающая префикс человекочитаемого номера письма."""

    __tablename__ = "mail_messagedirectory"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    number: Mapped[str] = mapped_column(String(10), unique=True)


class Message(Base):
    __tablename__ = "mail_message"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    custom_id: Mapped[str] = mapped_column(String(20), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE")
    )
    clients: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(255), default="")
    message_type: Mapped[str] = mapped_column(String(20), default="outgoing")
    message_rm_id: Mapped[int | None] = mapped_column(
        BIG_INT, ForeignKey("mail_messagerm.id", ondelete="SET NULL")
    )
    self_field: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[Profile] = relationship(lazy="joined")
    message_rm: Mapped[MessageRm | None] = relationship(lazy="joined")
    files: Mapped[list[MessageFile]] = relationship(
        back_populates="message", cascade="all, delete-orphan", lazy="selectin"
    )
    dirs: Mapped[list[MessageDir]] = relationship(secondary=message_dirs, lazy="selectin")
    masks: Mapped[list[MessageMask]] = relationship(secondary=message_masks, lazy="selectin")
    read_by: Mapped[list[Profile]] = relationship(secondary=message_read_by, lazy="selectin")
    starred_by: Mapped[list[Profile]] = relationship(secondary=message_starred_by, lazy="selectin")

    @property
    def is_deleted(self) -> bool:
        return self.message_rm_id is not None


class MessageFile(Base):
    __tablename__ = "mail_messagefile"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    file: Mapped[str] = mapped_column(String(100))
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("mail_message.id", ondelete="CASCADE")
    )
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())

    message: Mapped[Message] = relationship(back_populates="files")

    @property
    def filename(self) -> str:
        return self.file.rsplit("/", 1)[-1]


# --- Контакты ---------------------------------------------------------------


class ContactList(Base):
    __tablename__ = "mail_contactlist"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE")
    )
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("name", "user_id", name="mail_contactlist_name_user_id_uniq"),)


class Contact(Base):
    __tablename__ = "mail_contact"

    id: Mapped[int] = mapped_column(BIG_INT, primary_key=True, autoincrement=True)
    contact_list_id: Mapped[int] = mapped_column(
        BIG_INT, ForeignKey("mail_contactlist.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(255))
    contact_type: Mapped[str] = mapped_column(String(50), default="email")
    value: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("contact_list_id", "value", name="mail_contact_contact_list_id_value_uniq"),
    )


# --- Сессии -----------------------------------------------------------------


class Session(Base):
    """Серверная сессия пользователя.

    Единственная таблица, которую добавляет новое приложение (аддитивная
    миграция — существующие данные не затрагиваются).

    В cookie уходит только ``key``; данные пользователя не покидают сервер,
    поэтому подделать содержимое сессии, не имея доступа к БД, невозможно.
    Хранение в БД, а не в Redis, выбрано намеренно: сессии переживают
    перезапуск и очистку кеша, а нагрузка на них здесь пренебрежимо мала.
    """

    __tablename__ = "cbmail_session"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE, ForeignKey("useraccount_profile.id", ondelete="CASCADE"), index=True
    )
    auth_hash: Mapped[str] = mapped_column(String(64))
    """Отпечаток пароля: смена пароля делает все прежние сессии недействительными."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # Для аудита входов: разбор инцидента «кто и откуда зашёл» без этих полей
    # невозможен, а добавить их задним числом к уже утёкшей сессии — нельзя.
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(400))

    user: Mapped[Profile] = relationship(lazy="joined")
