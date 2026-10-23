from .petroline import PetrolineProvider
from .ukrnafta import UkrnaftaProvider
from .upg import UpgProvider

PROVIDERS = {
    "petroline": PetrolineProvider,
    "upg": UpgProvider,
    "ukrnafta": UkrnaftaProvider,
}
