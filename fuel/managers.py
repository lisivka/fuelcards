"""Відповідальні менеджери карток: користувачі системи з групи «Менеджери».

Групу створює міграція 0012; адміністратор додає в неї користувачів в адмінці.
Призначити картці можна лише активного користувача з групи; у фільтрах лишаються й ті,
кого прибрали з групи, але за ким ще записані картки.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q, QuerySet

MANAGERS_GROUP = "Менеджери"

# Фільтр «Менеджер»: "" — усі, MGR_NONE — без менеджера, MGR_ME — картки поточного користувача, інакше id
MGR_NONE = "none"
MGR_ME = "me"


def user_label(user) -> str:
    """ПІБ користувача або логін, якщо ПІБ не заповнене."""
    if user is None:
        return ""
    return user.get_full_name().strip() or user.get_username()


def _ordered(qs: QuerySet) -> QuerySet:
    return qs.order_by("last_name", "first_name", "username")


def assignable_managers() -> QuerySet:
    """Кого можна призначити картці: активні користувачі з групи «Менеджери»."""
    users = get_user_model().objects.filter(is_active=True, groups__name=MANAGERS_GROUP)
    return _ordered(users.distinct())


def known_managers() -> QuerySet:
    """Для фільтрів: менеджери з групи й усі, за ким записані картки."""
    users = get_user_model().objects.filter(Q(groups__name=MANAGERS_GROUP) | Q(managed_cards__isnull=False))
    return _ordered(users.distinct())


def parse_manager(value, current_id: int | None = None):
    """Менеджер з форми/JSON: порожнє — не призначено; лише менеджер із групи (або поточний картки)."""
    if value in (None, ""):
        return None
    try:
        pk = int(value)
    except (TypeError, ValueError):
        raise ValueError("Невідомий менеджер") from None
    users = get_user_model().objects
    allowed = assignable_managers().filter(pk=pk) if pk != current_id else users.filter(pk=pk)
    user = allowed.first()
    if user is None:
        raise ValueError("Користувач не є активним менеджером")
    return user


def parse_filter(value: str | None) -> str:
    """Значення фільтра «Менеджер»: "", MGR_NONE, MGR_ME або id відомого менеджера; решта — як «усі»."""
    value = (value or "").strip()
    if value in (MGR_NONE, MGR_ME):
        return value
    if value.isdigit() and known_managers().filter(pk=int(value)).exists():
        return value
    return ""


def filter_q(mgr: str, user, card_field: str = "card") -> Q:
    """Умова фільтра «Менеджер» для моделі з посиланням на картку (`card_field`)."""
    if mgr == MGR_NONE:
        return Q(**{f"{card_field}__manager__isnull": True})
    if mgr == MGR_ME:
        return Q(**{f"{card_field}__manager_id": user.pk})
    if mgr:
        return Q(**{f"{card_field}__manager_id": int(mgr)})
    return Q()


def audit_user_deleted(sender, instance, **kwargs) -> None:
    """Перед видаленням користувача: у його карток менеджер стане порожнім — пишемо це в журнал."""
    from .card_service import record_audit
    from .models import AuditLog

    label = user_label(instance)
    for card in instance.managed_cards.all():
        record_audit(AuditLog.Action.CARD_INFO, card, changes={"manager": [label, None]})


def filter_choices() -> list[tuple[str, str]]:
    return [(MGR_ME, "Мої"), (MGR_NONE, "Без менеджера")] + [
        (str(user.pk), user_label(user)) for user in known_managers()
    ]
