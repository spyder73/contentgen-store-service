from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AccessFeature, AccessFeatureUser, User
from ..schemas import AccessFeatureOut, UserAccessOut

DEFAULT_FEATURES: tuple[tuple[str, bool], ...] = (
    ("studio", False),
    ("generate", True),
    ("series", True),
    ("docs", True),
    ("builder", True),
    ("pipeline_manager", True),
    ("archive", True),
    ("upload_media", True),
)

TAB_FEATURES = {
    "studio": "studio",
    "generate": "generate",
    "series": "series",
    "docs": "docs",
    "builder": "builder",
    "pipeline": "pipeline_manager",
    "archive": "archive",
    "admin": "admin",
}


async def ensure_defaults(session: AsyncSession) -> None:
    for feature_key, whitelist_enabled in DEFAULT_FEATURES:
        row = await session.get(AccessFeature, feature_key)
        if row is None:
            session.add(
                AccessFeature(
                    feature_key=feature_key,
                    whitelist_enabled=whitelist_enabled,
                )
            )
    await session.flush()


async def _feature_user_ids(session: AsyncSession, feature_key: str) -> list[str]:
    result = await session.execute(
        select(AccessFeatureUser.user_id)
        .where(AccessFeatureUser.feature_key == feature_key)
        .order_by(AccessFeatureUser.user_id)
    )
    return list(result.scalars())


async def list_features(session: AsyncSession) -> list[AccessFeatureOut]:
    await ensure_defaults(session)
    result = await session.execute(select(AccessFeature).order_by(AccessFeature.feature_key))
    rows = list(result.scalars())
    return [
        AccessFeatureOut(
            feature_key=row.feature_key,
            whitelist_enabled=row.whitelist_enabled,
            user_ids=await _feature_user_ids(session, row.feature_key),
        )
        for row in rows
    ]


async def set_feature(session: AsyncSession, feature_key: str, whitelist_enabled: bool) -> AccessFeatureOut:
    await ensure_defaults(session)
    row = await session.get(AccessFeature, feature_key)
    if row is None:
        row = AccessFeature(feature_key=feature_key)
        session.add(row)
    row.whitelist_enabled = whitelist_enabled
    await session.commit()
    await session.refresh(row)
    return AccessFeatureOut(
        feature_key=row.feature_key,
        whitelist_enabled=row.whitelist_enabled,
        user_ids=await _feature_user_ids(session, row.feature_key),
    )


async def set_user_features(session: AsyncSession, user_id: str, feature_keys: list[str]) -> list[AccessFeatureOut]:
    await ensure_defaults(session)
    normalized = sorted({key.strip() for key in feature_keys if key and key.strip()})
    await session.execute(delete(AccessFeatureUser).where(AccessFeatureUser.user_id == user_id))
    for feature_key in normalized:
        if await session.get(AccessFeature, feature_key) is None:
            session.add(AccessFeature(feature_key=feature_key, whitelist_enabled=True))
            await session.flush()
        session.add(AccessFeatureUser(feature_key=feature_key, user_id=user_id))
    await session.commit()
    return await list_features(session)


async def get_user_access(session: AsyncSession, user_id: str) -> UserAccessOut | None:
    await ensure_defaults(session)
    user = await session.get(User, user_id)
    if user is None:
        return None
    features = await list_features(session)
    if user.is_admin:
        allowed = sorted({feature.feature_key for feature in features} | {"admin"})
    else:
        allowed = []
        for feature in features:
            if not feature.whitelist_enabled or user_id in feature.user_ids:
                allowed.append(feature.feature_key)
    visible_tabs = [
        tab
        for tab, feature_key in TAB_FEATURES.items()
        if feature_key in allowed or (tab == "admin" and user.is_admin)
    ]
    return UserAccessOut(
        user_id=user_id,
        is_admin=bool(user.is_admin),
        allowed_features=allowed,
        visible_tabs=visible_tabs,
    )
