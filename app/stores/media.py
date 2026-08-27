from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, bindparam, case, cast, delete, func, or_, select, text, Text, update
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, defer

from ..derivatives import is_image_content_type, make_micro_thumbnail, make_thumbnail
from ..models import Character, DatasetTemplate, MediaItem
from ..provenance import stamp_ai_provenance
from ..schemas import (
    MediaItemIn,
    MediaItemOut,
    MediaItemPatch,
    MediaRetentionOut,
    MediaStatsOut,
    PagedResponse,
    RelatedMediaOut,
)


# "Uploaded" vs "generated" is not a single literal source value. Manual uploads
# write metadata.source in this set; everything else (generated, render_output,
# the legacy "persisted" value, or no source at all) is treated as "generated".
# Mirrors the frontend's `isGeneratedSource` heuristic so the server-side filter
# and the client's mental model agree.
_UPLOAD_SOURCES = ("manual_upload", "upload", "upload_pool", "uploaded")

# Generated videos are stored with type "ai_video", but the library's type chip
# (and the per-type facet counts) speak of "video". Bucket both raw column values
# under the "video" facet so a ``type=video`` filter and the video count include
# AI-generated videos (otherwise "0 videos" shows even when ai_video rows exist).
_TYPE_BUCKETS: dict[str, tuple[str, ...]] = {
    "video": ("video", "ai_video"),
}


def _type_bucket_for(raw_type: str | None) -> str | None:
    """Map a raw ``type`` column value to its facet bucket (image/video/audio)."""
    if raw_type in ("video", "ai_video"):
        return "video"
    return raw_type


def _type_filter_expr(type_: str):
    """WHERE expression for a type facet — expands ``video`` to also match the
    ``ai_video`` rows generated videos are stored under."""
    values = _TYPE_BUCKETS.get(type_)
    if values:
        return MediaItem.type.in_(values)
    return MediaItem.type == type_


def _source_filter_expr(source: str):
    """Return a WHERE expression for the ``source`` bucket / literal.

    - ``uploaded`` → metadata.source IN the upload set.
    - ``generated`` → metadata.source NOT IN the upload set (includes NULL /
      missing source, so legacy rows count as generated rather than vanishing).
    - any other value → exact match on metadata.source (back-compat for callers
      that pass a concrete source string).
    """
    src = MediaItem.metadata_["source"].astext
    if source == "uploaded":
        return src.in_(_UPLOAD_SOURCES)
    if source == "generated":
        return src.is_(None) | src.notin_(_UPLOAD_SOURCES)
    return src == source


def _has_controlnet_filter_expr():
    """Match generated rows whose persisted recipe contains ControlNet inputs.

    Newer generation paths persist ``metadata.controls`` directly while older
    rows may only carry ``metadata.generation_recipe.controls``. Supporting both
    keeps the saved ControlNet preset useful across rolling deployments.
    """
    direct = MediaItem.metadata_["controls"].astext
    recipe = MediaItem.metadata_["generation_recipe"]["controls"].astext
    meaningful = lambda value: value.is_not(None) & value.notin_(("null", "[]", "{}", ""))
    return meaningful(direct) | meaningful(recipe)


def _order_by(sort: str):
    """Return a deterministic global ordering for a paged media query."""
    if sort == "oldest":
        return (MediaItem.created_at.asc(), MediaItem.id.asc())
    if sort == "favourite":
        return (
            MediaItem.is_favourite.desc(),
            MediaItem.created_at.desc(),
            MediaItem.id.desc(),
        )
    if sort == "name":
        return (
            case((MediaItem.name.is_(None), 1), else_=0).asc(),
            func.lower(MediaItem.name).asc(),
            MediaItem.created_at.desc(),
            MediaItem.id.desc(),
        )
    return (MediaItem.created_at.desc(), MediaItem.id.desc())


async def list_media(
    session: AsyncSession,
    clip_id: str | None = None,
    type_: str | None = None,
    search: str | None = None,
    is_favourite: bool | None = None,
    pipeline_run_id: str | None = None,
    scene_id: str | None = None,
    role: str | None = None,
    source: str | None = None,
    generator_profile_id: str | None = None,
    sort: str = "newest",
    created_after: datetime | None = None,
    has_controlnet: bool | None = None,
    page: int = 1,
    limit: int = 50,
    user_id: str | None = None,
) -> PagedResponse:
    if not user_id:
        raise ValueError("list_media requires user_id")
    offset = (page - 1) * limit
    # Defer the LargeBinary `file_data` and `thumbnail_data` BLOBs: the list
    # never serializes them (MediaItemOut excludes both) so reading them per row
    # only amplifies I/O and TOAST de-toasting. The deferred columns are never
    # touched in this path — MediaItemOut.from_orm_row reads only the small
    # mime-type columns — so no lazy-load is triggered.
    query = (
        select(MediaItem)
        .where(MediaItem.user_id == user_id)
        .options(defer(MediaItem.file_data), defer(MediaItem.thumbnail_data))
    )
    count_query = select(func.count()).select_from(MediaItem).where(MediaItem.user_id == user_id)

    if clip_id:
        query = query.where(MediaItem.clip_id == clip_id)
        count_query = count_query.where(MediaItem.clip_id == clip_id)
    if type_:
        type_expr = _type_filter_expr(type_)
        query = query.where(type_expr)
        count_query = count_query.where(type_expr)
    if search:
        pattern = f"%{search}%"
        search_filter = (
            MediaItem.prompt.ilike(pattern)
            | cast(MediaItem.id, Text).ilike(pattern)
            | MediaItem.name.ilike(pattern)
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    if is_favourite is not None:
        query = query.where(MediaItem.is_favourite == is_favourite)
        count_query = count_query.where(MediaItem.is_favourite == is_favourite)
    if pipeline_run_id:
        query = query.where(MediaItem.pipeline_run_id == pipeline_run_id)
        count_query = count_query.where(MediaItem.pipeline_run_id == pipeline_run_id)
    if scene_id:
        query = query.where(MediaItem.scene_id == scene_id)
        count_query = count_query.where(MediaItem.scene_id == scene_id)
    if role:
        query = query.where(MediaItem.role == role)
        count_query = count_query.where(MediaItem.role == role)

    if source:
        filter_expr = _source_filter_expr(source)
        query = query.where(filter_expr)
        count_query = count_query.where(filter_expr)

    if generator_profile_id:
        gen_profile_filter = MediaItem.metadata_["generator_profile_id"].astext == generator_profile_id
        query = query.where(gen_profile_filter)
        count_query = count_query.where(gen_profile_filter)

    if created_after is not None:
        query = query.where(MediaItem.created_at >= created_after)
        count_query = count_query.where(MediaItem.created_at >= created_after)

    if has_controlnet is not None:
        controlnet_filter = _has_controlnet_filter_expr()
        if not has_controlnet:
            controlnet_filter = ~controlnet_filter
        query = query.where(controlnet_filter)
        count_query = count_query.where(controlnet_filter)

    count_result = await session.execute(count_query)
    total = count_result.scalar_one()
    # The secondary `id` key makes the order TOTAL: ``created_at DESC`` alone is
    # ambiguous when many rows share a timestamp (a batch written in one
    # transaction), and Postgres may then return tied rows in a different order
    # per LIMIT/OFFSET query — so the same row can resurface on a later page
    # (page-1 items leaking into page N). Breaking the tie by `id DESC` makes
    # every page a deterministic slice of one global order: disjoint pages, no
    # duplicates. (See ix_media_items_user_created_desc — the index is on
    # user_id + created_at; id is appended in the sort, not the index.)
    result = await session.execute(
        query.order_by(*_order_by(sort))
        .offset(offset)
        .limit(limit)
    )
    items = [MediaItemOut.from_orm_row(row) for row in result.scalars()]
    return PagedResponse(items=items, total=total, page=page, limit=limit)


async def _get_owned(session: AsyncSession, id: str, user_id: str) -> MediaItem | None:
    row = await session.get(MediaItem, id)
    if row is None:
        return None
    if row.user_id is not None and row.user_id != user_id:
        return None
    return row


async def _get_by_id(session: AsyncSession, id: str, user_id: str | None) -> MediaItem | None:
    """Fetch a media row by id, optionally scoped to ``user_id``.

    Used by the byte-serving routes (file/thumbnail): those sit behind the
    X-Internal-Secret gate and are reached via the go-backend's public,
    unauthenticated embed path, which has no user context to forward. There
    the UUID id itself is the access control, so ``user_id=None`` returns
    the row unfiltered. When a caller DOES supply ``user_id`` it's still
    honored as an extra ownership filter, preserving the scoped lookup used
    by authenticated store callers (e.g. `_get_owned`).
    """
    row = await session.get(MediaItem, id)
    if row is None:
        return None
    if user_id and row.user_id is not None and row.user_id != user_id:
        return None
    return row


async def get_media(
    session: AsyncSession, id: str, user_id: str | None = None
) -> MediaItemOut | None:
    if not user_id:
        raise ValueError("get_media requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return None
    return MediaItemOut.from_orm_row(row)


# Cap how many siblings/variations the lineage view returns — the inspector
# shows a row of thumbnails, not an unbounded gallery.
_LINEAGE_LIMIT = 24


async def get_related_media(
    session: AsyncSession, id: str, user_id: str | None = None
) -> RelatedMediaOut | None:
    """Return the lineage of a media item: its parent, its co-variation siblings
    (sharing the same ``parent_media_id``), and the variations derived from it
    (rows whose ``parent_media_id`` points back at it).

    All results are scoped to ``user_id`` and exclude the item itself. Returns
    None when the item does not exist / is not owned; an item with no lineage
    yields an empty ``RelatedMediaOut`` (no parent, empty lists). BLOBs are
    deferred — the inspector renders thumbnails, never the originals.
    """
    if not user_id:
        raise ValueError("get_related_media requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return None

    deferred = (defer(MediaItem.file_data), defer(MediaItem.thumbnail_data))

    parent_out: MediaItemOut | None = None
    siblings: list[MediaItemOut] = []
    if row.parent_media_id:
        parent_row = await _get_owned(session, row.parent_media_id, user_id)
        if parent_row is not None:
            parent_out = MediaItemOut.from_orm_row(parent_row)
        # Siblings: same parent, not this item.
        sib_q = (
            select(MediaItem)
            .where(
                MediaItem.user_id == user_id,
                MediaItem.parent_media_id == row.parent_media_id,
                MediaItem.id != id,
            )
            .options(*deferred)
            .order_by(MediaItem.created_at.desc(), MediaItem.id.desc())
            .limit(_LINEAGE_LIMIT)
        )
        sib_res = await session.execute(sib_q)
        siblings = [MediaItemOut.from_orm_row(r) for r in sib_res.scalars()]

    # Variations: rows whose parent is this item.
    var_q = (
        select(MediaItem)
        .where(
            MediaItem.user_id == user_id,
            MediaItem.parent_media_id == id,
        )
        .options(*deferred)
        .order_by(MediaItem.created_at.desc(), MediaItem.id.desc())
        .limit(_LINEAGE_LIMIT)
    )
    var_res = await session.execute(var_q)
    variations = [MediaItemOut.from_orm_row(r) for r in var_res.scalars()]

    return RelatedMediaOut(parent=parent_out, siblings=siblings, variations=variations)


async def upsert_media(
    session: AsyncSession, body: MediaItemIn, user_id: str | None = None
) -> MediaItemOut:
    data = {
        "id": body.id,
        "clip_id": body.clip_id,
        "type": body.type,
        "prompt": body.prompt,
        "file_url": body.file_url,
        "metadata": body.metadata,
        "output_spec": body.output_spec,
        "name": body.name,
        "pipeline_run_id": body.pipeline_run_id,
        "scene_id": body.scene_id,
        "parent_media_id": body.parent_media_id,
        "role": body.role,
    }
    if user_id:
        data["user_id"] = user_id
    update_cols = {k: v for k, v in data.items() if k != "id"}
    stmt = (
        pg_insert(MediaItem.__table__)
        .values(**data)
        .on_conflict_do_update(index_elements=["id"], set_=update_cols)
    )
    await session.execute(stmt)
    await session.commit()
    row = await session.get(MediaItem, body.id, populate_existing=True)
    return MediaItemOut.from_orm_row(row)


async def patch_media(
    session: AsyncSession,
    id: str,
    body: MediaItemPatch,
    user_id: str | None = None,
) -> MediaItemOut | None:
    """Atomically patch one owned media row without replacing unrelated data."""
    if not user_id:
        raise ValueError("patch_media requires user_id")

    values: dict[object, object] = {}
    if body.file_url is not None:
        values[MediaItem.file_url] = body.file_url
    if body.metadata_merge:
        # PostgreSQL JSONB || performs a shallow key merge in the same UPDATE as
        # file_url. coalesce protects legacy NULL metadata rows. This is the
        # concurrency boundary required by the independent byte-persistence and
        # credit-settlement goroutines in the Go backend.
        values[MediaItem.metadata_] = func.coalesce(
            MediaItem.metadata_,
            cast({}, JSONB),
        ).op("||")(cast(body.metadata_merge, JSONB))

    if not values:
        return await get_media(session, id, user_id=user_id)

    stmt = (
        update(MediaItem)
        .where(
            MediaItem.id == id,
            (MediaItem.user_id == user_id) | (MediaItem.user_id.is_(None)),
        )
        .values(values)
        .returning(MediaItem.id)
    )
    result = await session.execute(stmt)
    if result.scalar_one_or_none() is None:
        await session.rollback()
        return None
    await session.commit()
    row = await session.get(MediaItem, id, populate_existing=True)
    if row is None:
        return None
    return MediaItemOut.from_orm_row(row)


async def delete_media(
    session: AsyncSession, id: str, user_id: str | None = None
) -> bool:
    if not user_id:
        raise ValueError("delete_media requires user_id")
    stmt = delete(MediaItem).where(
        MediaItem.id == id,
        (MediaItem.user_id == user_id) | (MediaItem.user_id.is_(None)),
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


async def toggle_favourite(
    session: AsyncSession, id: str, is_favourite: bool, user_id: str | None = None
) -> MediaItemOut | None:
    if not user_id:
        raise ValueError("toggle_favourite requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return None
    row.is_favourite = is_favourite
    await session.commit()
    await session.refresh(row)
    return MediaItemOut.from_orm_row(row)


async def rename_media(
    session: AsyncSession, id: str, name: str, user_id: str | None = None
) -> MediaItemOut | None:
    if not user_id:
        raise ValueError("rename_media requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return None
    row.name = name
    await session.commit()
    await session.refresh(row)
    return MediaItemOut.from_orm_row(row)


async def store_file_data(
    session: AsyncSession, id: str, data: bytes, mime_type: str, user_id: str | None = None
) -> bool:
    if not user_id:
        raise ValueError("store_file_data requires user_id")
    row = await _get_owned(session, id, user_id)
    if row is None:
        return False
    # AI-generated images (the backend stamps ai_generated into the row's
    # metadata before uploading bytes) get the XMP provenance packet embedded
    # in the file itself; everything else — uploads included — passes through
    # untouched. Gated inside by magic bytes, not mime, because generated
    # uploads may arrive as octet-stream.
    data = stamp_ai_provenance(data, row.metadata_)
    row.file_data = data
    row.file_mime_type = mime_type
    # Eagerly derive the grid thumbnail for images so the library never has to
    # load the full original. Non-images / undecodable bytes yield None → the
    # grid falls back to the original (no thumbnail advertised).
    if is_image_content_type(mime_type):
        thumb = make_thumbnail(data, mime_type)
        if thumb is not None:
            row.thumbnail_data, row.thumbnail_content_type = thumb
        else:
            # Clear any stale derivative if the new bytes can't be thumbnailed
            # (e.g. already small enough, or a re-upload with a different format).
            row.thumbnail_data = None
            row.thumbnail_content_type = None
        # The micro-thumb blur-up placeholder is produced even for small images
        # (it's a placeholder, not a payload optimisation). None → clear it.
        row.micro_thumbnail = make_micro_thumbnail(data, mime_type)
    else:
        row.micro_thumbnail = None
    await session.commit()
    return True


async def get_thumbnail(
    session: AsyncSession, id: str, user_id: str | None = None
) -> tuple[bytes, str] | None:
    """Return ``(bytes, content_type)`` for the item's grid thumbnail.

    ``user_id`` is optional here (unlike most other media accessors): this
    backs a byte-serving route reached via the go-backend's public embed
    path with no user context, where the id itself is the access gate. When
    ``user_id`` is supplied it's still enforced as an ownership filter — see
    `_get_by_id`.

    Lazy backfill: if no derivative exists yet but the row is an image with
    stored bytes, generate the thumbnail on this first GET and persist it so the
    next request is served from the column. Returns None when no thumbnail can
    be produced (caller falls back to the original).
    """
    row = await _get_by_id(session, id, user_id)
    if row is None:
        return None
    if row.thumbnail_data is not None and row.thumbnail_content_type:
        # Opportunistically backfill the micro-thumb for legacy rows that have a
        # full thumbnail but predate the 0018 column. Cheap (a few hundred bytes)
        # and saves the list path from ever needing the BLOB.
        if row.micro_thumbnail is None and row.file_data is not None:
            micro = make_micro_thumbnail(row.file_data, row.file_mime_type)
            if micro is not None:
                row.micro_thumbnail = micro
                await session.commit()
        return row.thumbnail_data, row.thumbnail_content_type
    # No derivative yet — attempt lazy backfill from the stored original.
    if row.file_data is None or not is_image_content_type(row.file_mime_type):
        return None
    thumb = make_thumbnail(row.file_data, row.file_mime_type)
    if thumb is None:
        # Even when no full thumbnail is warranted (image already small enough),
        # the micro-thumb placeholder is still useful — backfill it lazily.
        if row.micro_thumbnail is None:
            micro = make_micro_thumbnail(row.file_data, row.file_mime_type)
            if micro is not None:
                row.micro_thumbnail = micro
                await session.commit()
        return None
    row.thumbnail_data, row.thumbnail_content_type = thumb
    if row.micro_thumbnail is None:
        row.micro_thumbnail = make_micro_thumbnail(row.file_data, row.file_mime_type)
    await session.commit()
    return thumb


async def store_proxy_data(
    session: AsyncSession, id: str, data: bytes, mime_type: str, user_id: str | None = None
) -> bool:
    """Persist the 480p transcoded proxy derivative (video editor, D5).

    Mirrors `store_file_data`'s persistence shape but has no derivation step —
    the Go backend does the ffmpeg transcode and PUTs the finished bytes here
    as a best-effort cache-fill so other instances / restarts can reuse it.
    `user_id` is optional here (unlike `store_file_data`): the Go side may
    upload from a background singleflight fill with no request-scoped user
    context, mirroring `get_thumbnail`/`_get_by_id`'s optional-ownership
    semantics for byte-serving routes.
    """
    row = await _get_by_id(session, id, user_id)
    if row is None:
        return False
    row.proxy_bytes = data
    row.proxy_mime = mime_type
    await session.commit()
    return True


async def get_proxy(
    session: AsyncSession, id: str, user_id: str | None = None
) -> tuple[bytes, str] | None:
    """Return ``(bytes, content_type)`` for the item's 480p proxy derivative.

    ``user_id`` is optional — see `get_thumbnail` / `_get_by_id` for why this
    byte-serving accessor is not ownership-required like the rest of the
    media store. Returns None when no proxy has been generated yet (caller
    falls back to the original / triggers on-demand generation).
    """
    row = await _get_by_id(session, id, user_id)
    if row is None or row.proxy_bytes is None:
        return None
    return row.proxy_bytes, (row.proxy_mime or "video/mp4")


async def get_file_data(
    session: AsyncSession, id: str, user_id: str | None = None
) -> tuple[bytes, str] | None:
    """Return ``(bytes, mime_type)`` for the item's stored original.

    ``user_id`` is optional — see `get_thumbnail` / `_get_by_id` for why this
    byte-serving accessor is not ownership-required like the rest of the
    media store.
    """
    row = await _get_by_id(session, id, user_id)
    if row is None or row.file_data is None:
        return None
    return row.file_data, (row.file_mime_type or "application/octet-stream")


async def get_media_stats(session: AsyncSession, user_id: str | None = None) -> MediaStatsOut:
    # Library-wide counts per type AND per source bucket. These are the canonical
    # facet totals the UI shows on the type/source chips — derived here over the
    # WHOLE library so a type/source absent from the current page never reads as
    # "0 videos". Counting both facets in one grouped pass keeps it a single scan.
    src = MediaItem.metadata_["source"].astext
    query = select(MediaItem.type, src.label("source"), func.count().label("cnt"))
    if user_id:
        query = query.where(MediaItem.user_id == user_id)
    query = query.group_by(MediaItem.type, src)
    result = await session.execute(query)

    type_counts: dict[str, int] = {}
    uploaded = 0
    generated = 0
    total = 0
    for row in result:
        # Fold ai_video into the "video" facet so generated videos are counted.
        bucket = _type_bucket_for(row.type)
        if bucket:
            type_counts[bucket] = type_counts.get(bucket, 0) + row.cnt
        if row.source in _UPLOAD_SOURCES:
            uploaded += row.cnt
        else:  # generated bucket: everything else, including NULL/missing source
            generated += row.cnt
        total += row.cnt

    return MediaStatsOut(
        total=total,
        image=type_counts.get("image", 0),
        video=type_counts.get("video", 0),
        audio=type_counts.get("audio", 0),
        uploaded=uploaded,
        generated=generated,
    )


# ── retention (disk hygiene) ─────────────────────────────────────────────────

# A finished clip render is the ONE media class whose bytes are reproducible:
# the scene images, the prompts and the render template that produced it all
# stay in the database, so the clip can simply be re-rendered. Every other row
# is an original whose bytes exist nowhere else. Rendered clips are written with
# metadata.source = "render_output"; nothing else is eligible.
_RENDER_OUTPUT_SOURCE = "render_output"


def _uuid_text(expr):
    """Normalise an id expression for text comparison (lowercase, no hyphens)."""
    return func.replace(func.lower(expr), "-", "")


def _retention_protected_clause():
    """Rows whose bytes must survive the sweep even if they look like old renders.

    Each clause guards a reference that resolves a media id with nothing to fall
    back on. What breaks if one is dropped:

    * ``is_favourite`` — the user's explicit "keep this" flag. Stripping it
      empties the one shelf they curate by hand, with no way to tell which items
      were lost.
    * ``characters.reference_image_media_id`` — a real FK, but ``ON DELETE SET
      NULL`` only protects the row from DELETE; it does nothing about an UPDATE
      that nulls the bytes. Losing them leaves the character with an id that
      resolves to an empty item, so every later generation loses its identity
      reference.
    * ``dataset_templates.seed_reference_media_id`` — a plain Text column with
      NO foreign key and no index, so nothing in the schema marks it as a media
      reference. It is the identity anchor chained through every collage stage
      of a LoRA dataset; strip it and that dataset can never be regenerated
      consistently again.
    * being another row's ``parent_media_id`` — the parent is the original a
      variation/edit was derived from. Children point back at it for lineage and
      for re-deriving; strip the parent and the whole branch loses its source.

    Written as NOT EXISTS rather than NOT IN on purpose: two of these columns are
    nullable, and a single NULL inside a ``NOT IN`` subquery makes the predicate
    NULL for EVERY row — silently turning the sweep into a no-op (or, with the
    operands the other way round, into a sweep that strips protected rows).
    NOT EXISTS has no such NULL trap.
    """
    child = aliased(MediaItem)
    return and_(
        # is_not(True) rather than == False so legacy NULL rows count as
        # protected rather than as "not favourited".
        MediaItem.is_favourite.is_not(True),
        ~select(1).where(Character.reference_image_media_id == MediaItem.id).exists(),
        # seed_reference_media_id is untyped Text while media_items.id is a UUID,
        # so the comparison has to happen as text. Both sides are normalised
        # (lowercase, hyphens removed) because nothing constrains what shape of
        # id that column holds — and here a false match merely over-protects a
        # row, while a missed match destroys a dataset's identity anchor.
        ~select(1)
        .where(
            _uuid_text(DatasetTemplate.seed_reference_media_id)
            == _uuid_text(cast(MediaItem.id, Text))
        )
        .exists(),
        ~select(1).where(child.parent_media_id == MediaItem.id).exists(),
    )


def _expired_render_clause(cutoff: datetime):
    """Render outputs older than ``cutoff`` that still hold bytes and are unprotected.

    The "still holds bytes" term is what makes the sweep idempotent: once a row
    has been stripped it no longer matches, so a re-run neither rewrites it nor
    counts it again.
    """
    return and_(
        MediaItem.metadata_["source"].astext == _RENDER_OUTPUT_SOURCE,
        MediaItem.created_at < cutoff,
        or_(
            MediaItem.file_data.is_not(None),
            MediaItem.thumbnail_data.is_not(None),
            MediaItem.micro_thumbnail.is_not(None),
        ),
        _retention_protected_clause(),
    )


def _stripped_bytes_expr():
    """Bytes reclaimed per stripped row.

    ``length()`` over a bytea counts bytes; over ``micro_thumbnail`` (a base64
    ``data:`` URI, so ASCII) characters and bytes coincide. It is an estimate of
    logical size, not of the on-disk footprint TOAST actually releases.
    """
    return (
        func.coalesce(func.length(MediaItem.file_data), 0)
        + func.coalesce(func.length(MediaItem.thumbnail_data), 0)
        + func.coalesce(func.length(MediaItem.micro_thumbnail), 0)
    )


async def sweep_media_retention(
    session: AsyncSession, *, older_than_days: int, dry_run: bool = True
) -> MediaRetentionOut:
    """Drop reproducible bytes from media rows older than ``older_than_days``.

    Two independent effects, both in one transaction:

    1. Rendered clips past the cutoff lose ``file_data`` / ``thumbnail_data`` /
       ``micro_thumbnail`` and get ``bytes_evicted: true`` merged into their
       metadata, so the UI can offer "re-render" instead of a dead player.
    2. ``proxy_bytes`` is cleared on ANY row past the cutoff, protected or not:
       the proxy is a pure cache the Go backend regenerates on demand (it serves
       a redirect to the original on a miss — see
       ``internal/api/media/serveProxy.go``), so nothing is lost by dropping it.

    Rows are never deleted. Dozens of places hold loose media ids with no
    foreign key (``clip_prompts.media_refs``, ``render_output_urls``,
    ``review_variants``, ``variant_of``, thumbnails); a DELETE would dangle all
    of them, while a nulled byte column leaves every reference resolvable.

    Safe to run repeatedly and concurrently: the eligibility predicate excludes
    already-stripped rows, and the row lock is taken with SKIP LOCKED so a
    second concurrent sweep works on the rows the first one has not claimed
    instead of blocking on them. Two overlapping sweeps can still contend on the
    proxy UPDATE; the loser aborts and loses nothing, since the next run finds
    exactly the same work.
    """
    if older_than_days < 1:
        raise ValueError("older_than_days must be >= 1")

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    strip_where = _expired_render_clause(cutoff)
    proxy_where = and_(MediaItem.created_at < cutoff, MediaItem.proxy_bytes.is_not(None))

    proxies_cleared, proxy_bytes_freed = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(func.length(MediaItem.proxy_bytes)), 0),
            ).where(proxy_where)
        )
    ).one()

    if dry_run:
        stripped_rows, stripped_bytes = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(_stripped_bytes_expr()), 0),
                ).where(strip_where)
            )
        ).one()
        # Nothing above wrote, but end the transaction explicitly so a dry run
        # can never leave an idle-in-transaction connection holding snapshots.
        await session.rollback()
        return MediaRetentionOut(
            dry_run=True,
            older_than_days=older_than_days,
            cutoff=cutoff,
            stripped_rows=stripped_rows,
            stripped_bytes=int(stripped_bytes),
            proxies_cleared=proxies_cleared,
            proxy_bytes_freed=int(proxy_bytes_freed),
        )

    # Selecting id/metadata/size (never the blobs themselves) keeps a sweep over
    # a 20 GB table off the Python heap, and gives the exact metadata to merge
    # into. FOR UPDATE holds the claimed rows for the rest of the transaction so
    # a concurrent writer cannot land a metadata change we would then overwrite.
    candidates = (
        await session.execute(
            select(MediaItem.id, MediaItem.metadata_, _stripped_bytes_expr().label("freed"))
            .where(strip_where)
            .order_by(MediaItem.id)
            .with_for_update(of=MediaItem, skip_locked=True)
        )
    ).all()

    stripped_bytes = sum(int(row.freed or 0) for row in candidates)
    if candidates:
        # Addressed at the Table rather than the mapped class: a per-row bound
        # WHERE is a plain executemany, which the ORM update path rejects.
        table = MediaItem.__table__
        await session.execute(
            update(table)
            .where(table.c.id == bindparam("b_id"))
            .values(
                {
                    table.c.file_data: None,
                    table.c.thumbnail_data: None,
                    table.c.micro_thumbnail: None,
                    table.c["metadata"]: bindparam("b_metadata", type_=JSONB),
                }
            ),
            [
                {
                    "b_id": row.id,
                    # Merge, never replace: the row's provenance, source and
                    # generation recipe are all that survives the eviction.
                    "b_metadata": {**(row.metadata_ or {}), "bytes_evicted": True},
                }
                for row in candidates
            ],
        )

    # proxy_mime is left in place: it is a few bytes of text, and get_proxy keys
    # off proxy_bytes being NULL, so a stale mime is inert.
    await session.execute(update(MediaItem).where(proxy_where).values(proxy_bytes=None))
    await session.commit()

    return MediaRetentionOut(
        dry_run=False,
        older_than_days=older_than_days,
        cutoff=cutoff,
        stripped_rows=len(candidates),
        stripped_bytes=stripped_bytes,
        proxies_cleared=proxies_cleared,
        proxy_bytes_freed=int(proxy_bytes_freed),
    )
