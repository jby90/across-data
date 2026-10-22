"""导入所有 ORM 模型,保证任何入口(API、worker、CLI、迁移)里外键都能解析。

新增模型模块时必须在这里登记。
"""

from app.analysis import models as analysis_models  # noqa: F401
from app.iam import models as iam_models  # noqa: F401
from app.ingestion import models as ingestion_models  # noqa: F401
from app.knowledge import models as knowledge_models  # noqa: F401
from app.sources import models as sources_models  # noqa: F401
