from dataclasses import dataclass
from typing import Any, Optional, Tuple

from dbt.adapters.protocol import AdapterConfig
from dbt.adapters.sqlserver.relation_configs import SQLServerIndexConfig


@dataclass
class SQLServerConfigs(AdapterConfig):
    auto_provision_aad_principals: Optional[bool] = False
    indexes: Optional[Tuple[SQLServerIndexConfig]] = None
    # false (default) | warn | true
    drop_unmanaged_indexes: Optional[Any] = False
