# models 包：SQLAlchemy ORM 数据模型
# 导入各模型类以注册到 Base.metadata，确保 create_all 可建表。
from app.models.billing_subscription import BillingEvent, Subscription
from app.models.billing_finance import LedgerEntry, Reconciliation, RiskEvent
from app.models.billing_order import Invoice, Order
from app.models.billing_org import Organization, OrganizationMember
from app.models.billing_plan import Plan, PlanFeature
from app.models.billing_promotion import Promotion, PromotionRedemption
from app.models.billing_quota import QuotaAccount, QuotaLedger
from app.models.ai_quota import AIQuota
from app.models.ai_task import AITask
from app.models.ai_task_token_usage import AITaskTokenUsage
from app.models.api_token import ApiToken
from app.models.developer_app import ApiCallLog, DeveloperApp
from app.models.mobile_draft import MobileDraft
from app.models.openapi_ai_task import OpenAPIAITask
from app.models.webhook import WebhookDelivery, WebhookSubscription
from app.models.ai_usage_daily_stat import AIFailureStat, AIUsageDailyStat
from app.models.ai_usage_log import AIUsageLog
from app.models.audit_log import AuditLog
from app.models.cache_entry import CacheEntry
from app.models.chapter_content import ChapterContent
from app.models.chapter_fact_dependency import ChapterFactDependency
from app.models.chapter_memory import ChapterMemory
from app.models.chapter_version import ChapterVersion
from app.models.character import Character
from app.models.character_state_snapshot import CharacterStateSnapshot
from app.models.character_relationship import CharacterRelationship
from app.models.change_suggestion import ChangeSuggestion
from app.models.collaboration import ActivityLog, ProjectInvitation, ProjectMember
from app.models.comment import Comment, CommentRateLimit, Mention
from app.models.confirmation_token import ConfirmationToken
from app.models.consistency_review import ConsistencyReview
from app.models.content_daily_stat import ContentDailyStat
from app.models.content_version import ContentVersion
from app.models.drill_record import DrillRecord
from app.models.export_task import ExportTask
from app.models.faction import Faction
from app.models.foreshadow import Foreshadow
from app.models.favorite import Favorite
from app.models.featured_content import FeaturedContent
from app.models.generation import GenerationRecord
from app.models.fact_issue_disposition import DISPOSITION_TYPES, FactIssueDisposition
from app.models.idempotency_record import IdempotencyRecord
from app.models.import_task import ImportTask
from app.models.item import Item
from app.models.item_possession import ItemPossession
from app.models.job import Job
from app.models.location import Location
from app.models.notification import Notification, NotificationPreference
from app.models.novel_plan_job import NovelPlanJob
from app.models.outbox_event import OutboxEvent
from app.models.outline import Outline
from app.models.outline_plan_version import OutlinePlanVersion
from app.models.performance_metric import PerformanceMetric
from app.models.private_access_grant import PrivateAccessGrant
from app.models.project_backup import ProjectBackup
from app.models.project import Project
from app.models.project_setting import ProjectSetting
from app.models.publication import Publication
from app.models.rate_limit import RateLimitRecord
from app.models.reading_event import ReadingEvent
from app.models.refresh_token import RefreshToken
from app.models.relationship import Relationship
from app.models.report import Report
from app.models.security_event import SecurityEvent
from app.models.stored_file import StoredFile
from app.models.support_ticket import SupportTicket
from app.models.system_metric import SystemMetric
from app.models.timeline_event import TimelineEvent
from app.models.world_rule import WorldRule
from app.models.reader import ReaderPreference, ReadingProgress
from app.models.recent_item import RecentItem
from app.models.search_document import SearchDocument
from app.models.search_history import SearchHistory
from app.models.secret_knower import SecretKnower
from app.models.tag import OutlineTag, Tag, TagRelation
from app.models.user import User
from app.models.world_setting import WorldSetting
from app.models.writing_daily_stat import WritingDailyStat
from app.models.writing_goal import WritingGoal

# 第二十一批「国际化、多语言和多区域部署」
from app.models.cdn_asset import CdnAsset, DownloadLog
from app.models.compliance import ConsentRecord, DataRequest, PolicyVersion
from app.models.currency import Currency
from app.models.data_region import DataRegion, RegionAccessLog
from app.models.i18n_resource import I18nResource, I18nResourceVersion
from app.models.notification_template import NotificationTemplate
from app.models.plan_price import PlanPrice
from app.models.project_translation import ChapterTranslation, ProjectTranslation
from app.models.region_event import RegionEvent
from app.models.region_monitor import RegionMetric, RegionStatus
from app.models.tax_record import TaxRecord
from app.models.translation_glossary import TranslationGlossary
from app.models.user_locale import UserLocale

# 第二十二批「数据分析、推荐系统和智能运营」
from app.models.analytics_event import AnalyticsEvent
from app.models.creation_suggestion import CreationSuggestion, SuggestionFeedback, SuggestionPreference
from app.models.data_privacy_pref import DataPrivacyPref
from app.models.data_quality import AnalyticsDeadLetter, DataQualityMetric
from app.models.experiment import Experiment, ExperimentAssignment, ExperimentEvent
from app.models.operation_flow import OperationFlow, OperationMessageLog
from app.models.recommend_daily_stat import RecommendDailyStat
from app.models.recommendation import BlockedRecommend, RecommendAction, RecommendLog
from app.models.user_segment import UserSegment

# 第二十四批「企业级协作、审核与内容治理」
from app.models.enterprise_org import Workspace, OrgInvitation
from app.models.enterprise_collab import EditLock, ReviewTask
from app.models.enterprise_governance import (
    ContentReview,
    ContentGovernanceRecord,
    CopyrightRecord,
    CopyrightClaim,
    HighRiskActionRecord,
)
from app.models.enterprise_file import FileShareLink, FileAccessLog
from app.models.enterprise_security import EnterpriseSecurityPolicy, AIEnterpriseConfig

__all__ = [
    "AIFailureStat",
    "AIQuota",
    "BillingEvent",
    "ApiCallLog",
    "ApiToken",
    "AIUsageDailyStat",
    "AIUsageLog",
    "AITask",
    "AITaskTokenUsage",
    "NovelPlanJob",
    "ActivityLog",
    "AuditLog",
    "CacheEntry",
    "ChapterContent",
    "ChapterFactDependency",
    "ChapterMemory",
    "ChapterVersion",
    "Character",
    "CharacterStateSnapshot",
    "CharacterRelationship",
    "ChangeSuggestion",
    "Comment",
    "CommentRateLimit",
    "ConfirmationToken",
    "ContentDailyStat",
    "ContentVersion",
    "DeveloperApp",
    "DrillRecord",
    "ExportTask",
    "Favorite",
    "Faction",
    # 第一阶段「事实数据库」：误报处置记录（导出须与导入/__all__ 一致）
    "FactIssueDisposition",
    "DISPOSITION_TYPES",
    "FeaturedContent",
    "GenerationRecord",
    "IdempotencyRecord",
    "ImportTask",
    "Item",
    "ItemPossession",
    "Invoice",
    "Job",
    "LedgerEntry",
    "Location",
    "Mention",
    "MobileDraft",
    "Notification",
    "NotificationPreference",
    "OpenAPIAITask",
    "Order",
    "Organization",
    "OrganizationMember",
    "OutboxEvent",
    "Outline",
    "OutlineTag",
    "OutlinePlanVersion",
    "PerformanceMetric",
    "Plan",
    "PlanFeature",
    "PrivateAccessGrant",
    "Project",
    "ProjectBackup",
    "ProjectInvitation",
    "ProjectMember",
    "ProjectSetting",
    "Promotion",
    "PromotionRedemption",
    "Publication",
    "QuotaAccount",
    "QuotaLedger",
    "RateLimitRecord",
    "ReaderPreference",
    "ReadingEvent",
    "ReadingProgress",
    "RecentItem",
    "Reconciliation",
    "RefreshToken",
    "Relationship",
    "Report",
    "RiskEvent",
    "SearchDocument",
    "SearchHistory",
    "SecretKnower",
    "SecurityEvent",
    "StoredFile",
    "Subscription",
    "SupportTicket",
    "SystemMetric",
    "Tag",
    "TagRelation",
    "TimelineEvent",
    "User",
    "WebhookDelivery",
    "WebhookSubscription",
    "WorldRule",
    "WorldSetting",
    "WritingDailyStat",
    "WritingGoal",
    # 第二十一批
    "CdnAsset",
    "DownloadLog",
    "ConsentRecord",
    "DataRequest",
    "PolicyVersion",
    "Currency",
    "DataRegion",
    "RegionAccessLog",
    "I18nResource",
    "I18nResourceVersion",
    "NotificationTemplate",
    "PlanPrice",
    "ChapterTranslation",
    "ProjectTranslation",
    "RegionEvent",
    "RegionMetric",
    "RegionStatus",
    "TaxRecord",
    "TranslationGlossary",
    "UserLocale",
    # 第二十二批
    "AnalyticsEvent",
    "CreationSuggestion",
    "SuggestionFeedback",
    "SuggestionPreference",
    "DataPrivacyPref",
    "AnalyticsDeadLetter",
    "DataQualityMetric",
    "Experiment",
    "ExperimentAssignment",
    "ExperimentEvent",
    "OperationFlow",
    "OperationMessageLog",
    "RecommendDailyStat",
    "BlockedRecommend",
    "RecommendAction",
    "RecommendLog",
    "UserSegment",
    # 第二十四批
    "Workspace",
    "OrgInvitation",
    "EditLock",
    "ReviewTask",
    "ContentReview",
    "ContentGovernanceRecord",
    "CopyrightRecord",
    "CopyrightClaim",
    "HighRiskActionRecord",
    "FileShareLink",
    "FileAccessLog",
    "EnterpriseSecurityPolicy",
    "AIEnterpriseConfig",
    # 第四期「写错能发现」：伏笔追踪
    "Foreshadow",
    # 阶段I：独立 LLM 连续性审稿报告（只读，不改正文）
    "ConsistencyReview",
]
