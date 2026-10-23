from django.urls import path

from . import card_views, views

urlpatterns = [
    path("", views.transactions, name="transactions"),
    path("cards/", card_views.cards, name="cards"),
    path("cards/create/", card_views.card_create, name="card_create"),
    path("cards/sync/", card_views.cards_sync, name="cards_sync"),
    path("cards/<int:pk>/info/", card_views.card_info, name="card_info"),
    path("cards/<int:pk>/status/", card_views.card_status, name="card_status"),
    path("cards/<int:pk>/delete/", card_views.card_delete, name="card_delete"),
]
