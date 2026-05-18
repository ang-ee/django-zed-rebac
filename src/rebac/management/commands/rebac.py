"""Single management command with subcommands.

python manage.py rebac sync                     # idempotent
python manage.py rebac sync --check             # CI gate; non-zero on drift
python manage.py rebac sync --force-overwrite   # destructive
python manage.py rebac check                    # validate without writes
python manage.py rebac build-zed                # emit effective.zed
python manage.py rebac explain <type>.<perm>    # print compiled expression
python manage.py rebac migrate-storage --to registry   # proposal 0001 cutover
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ...schema.parser import parse_zed, validate_schema


class Command(BaseCommand):
    help = "Manage rebac schema (sync / check / build-zed / explain)."

    def add_arguments(self, parser: Any) -> None:
        sub = parser.add_subparsers(dest="cmd", required=True)

        p_sync = sub.add_parser("sync", help="Load permissions.zed files into Schema* tables")
        p_sync.add_argument(
            "--check", action="store_true", help="CI gate: detect drift; no writes."
        )
        p_sync.add_argument(
            "--force-overwrite",
            action="store_true",
            help="Bypass no_update; destructive.",
        )
        p_sync.add_argument("--package", help="Limit to one package")
        p_sync.add_argument("--yes", action="store_true", help="Skip confirmation")

        sub.add_parser("check", help="Validate without writing.")

        p_build = sub.add_parser("build-zed", help="Emit unified effective.zed")
        p_build.add_argument("--out", default=None)

        p_explain = sub.add_parser("explain", help="Print compiled expression for <type>.<perm>")
        p_explain.add_argument("target", help="e.g. blog/post.read")

        p_mig = sub.add_parser(
            "migrate-storage",
            help="Proposal 0001 — copy rows between denormalized and registry shapes.",
        )
        p_mig.add_argument(
            "--from",
            dest="src",
            choices=("denormalized", "registry"),
            default=None,
            help="Source shape (default: opposite of --to).",
        )
        p_mig.add_argument(
            "--to",
            dest="dst",
            choices=("denormalized", "registry"),
            required=True,
            help="Destination shape.",
        )
        p_mig.add_argument("--batch", type=int, default=None, help="Override batch size.")
        p_mig.add_argument("--dry-run", action="store_true", help="Report counts without writes.")

    def handle(self, *args: Any, **options: Any) -> None:
        cmd = options["cmd"]
        if cmd == "sync":
            self._handle_sync(options)
        elif cmd == "check":
            self._handle_check()
        elif cmd == "build-zed":
            self._handle_build_zed(options)
        elif cmd == "explain":
            self._handle_explain(options)
        elif cmd == "migrate-storage":
            self._handle_migrate_storage(options)
        else:
            raise CommandError(f"Unknown subcommand: {cmd}")

    # ---------- sync ----------

    def _handle_sync(self, options: dict[str, Any]) -> None:
        from django.db import transaction

        from ...models import (
            SchemaCaveat,
            SchemaDefinition,
            SchemaPermission,
            SchemaRelation,
        )

        check_only = options["check"]
        force = options["force_overwrite"]
        only_package = options.get("package")

        any_drift = False
        for app_config in apps.get_app_configs():
            schema_path = self._resolve_schema_path(app_config)
            if schema_path is None:
                continue
            package_name = app_config.name
            if only_package and only_package != package_name:
                continue

            text = schema_path.read_text(encoding="utf-8")
            schema = parse_zed(text)
            errors = validate_schema(schema)
            if errors:
                for e in errors:
                    self.stderr.write(self.style.ERROR(f"  {package_name}: {e}"))
                raise CommandError(f"Schema validation failed for {package_name}")

            self.stdout.write(f"-> {package_name} ({schema_path})")

            with transaction.atomic():
                # Caveats first (definitions reference them).
                for caveat in schema.caveats:
                    payload = {
                        "params": [{"name": p.name, "type": p.type} for p in caveat.params],
                        "expression": caveat.expression,
                    }
                    drift = self._sync_row(
                        SchemaCaveat,
                        natural_key={"name": caveat.name},
                        payload=payload,
                        package=package_name,
                        external_id=f"caveat:{caveat.name}",
                        check_only=check_only,
                        force=force,
                    )
                    any_drift = any_drift or drift

                # Definitions
                for d in schema.definitions:
                    drift = self._sync_row(
                        SchemaDefinition,
                        natural_key={"resource_type": d.resource_type},
                        payload={},
                        package=package_name,
                        external_id=f"definition:{d.resource_type}",
                        check_only=check_only,
                        force=force,
                    )
                    any_drift = any_drift or drift
                    if check_only:
                        continue
                    schema_def = SchemaDefinition.objects.get(resource_type=d.resource_type)

                    for r in d.relations:
                        allowed = [
                            {
                                "type": s.type,
                                "relation": s.relation,
                                "wildcard": s.wildcard,
                                "with_caveat": s.with_caveat,
                                "id": s.id,
                            }
                            for s in r.allowed_subjects
                        ]
                        SchemaRelation.objects.update_or_create(
                            definition=schema_def,
                            name=r.name,
                            defaults={
                                "allowed_subjects": allowed,
                                "caveat": "",
                                "with_expiration": r.with_expiration,
                            },
                        )

                    for p in d.permissions:
                        SchemaPermission.objects.update_or_create(
                            definition=schema_def,
                            name=p.name,
                            defaults={"expression": p.raw_text or "<expr>"},
                        )

        if check_only:
            if any_drift:
                raise CommandError("Schema drift detected. Run `rebac sync` to apply.")
            self.stdout.write(self.style.SUCCESS("OK — no drift."))
            return

        # Reset cached backend so the next access reloads the schema from DB.
        from ...backends import reset_backend

        reset_backend()
        self.stdout.write(self.style.SUCCESS("Sync complete."))

    def _resolve_schema_path(self, app_config: Any) -> Path | None:
        # Two ways to declare: `AppConfig.rebac_schema = "permissions.zed"` (rel
        # path), or a `permissions.zed` adjacent to apps.py.
        rel = getattr(app_config, "rebac_schema", None)
        if rel is not None:
            path = Path(app_config.path) / rel
        else:
            path = Path(app_config.path) / "permissions.zed"
        return path if path.exists() else None

    def _sync_row(
        self,
        model_cls: type,
        natural_key: dict[str, Any],
        payload: dict[str, Any],
        package: str,
        external_id: str,
        check_only: bool,
        force: bool,
    ) -> bool:
        """Sync one schema row, respecting `no_update`. Returns True if drifted."""
        from django.contrib.contenttypes.models import ContentType

        from ...models import PackageManagedRecord

        content_hash = self._hash_payload({**natural_key, **payload})
        existing = model_cls.objects.filter(**natural_key).first()
        ct = ContentType.objects.get_for_model(model_cls)

        record = PackageManagedRecord.objects.filter(
            package=package, external_id=external_id
        ).first()

        if existing is None:
            # Fresh.
            if check_only:
                return True
            obj = model_cls.objects.create(**natural_key, **payload)
            PackageManagedRecord.objects.update_or_create(
                package=package,
                external_id=external_id,
                defaults={
                    "schema_revision": 1,
                    "target_ct": ct,
                    "target_pk": obj.pk,
                    "content_hash": content_hash,
                    "no_update": True,
                    "last_synced_at": timezone.now(),
                },
            )
            return False

        # Already present. Decide based on hash + no_update.
        if record is None:
            # Orphan adoption — claim ownership.
            if check_only:
                return True
            for k, v in payload.items():
                setattr(existing, k, v)
            existing.save()
            PackageManagedRecord.objects.create(
                package=package,
                external_id=external_id,
                schema_revision=1,
                target_ct=ct,
                target_pk=existing.pk,
                content_hash=content_hash,
                no_update=True,
                last_synced_at=timezone.now(),
            )
            return False

        if record.content_hash == content_hash:
            return False  # no-op

        if record.no_update and not force:
            self.stdout.write(
                self.style.WARNING(
                    f"  ! drift: {external_id} (admin-edited; --force-overwrite to apply)"
                )
            )
            return True

        if check_only:
            return True
        for k, v in payload.items():
            setattr(existing, k, v)
        existing.save()
        record.content_hash = content_hash
        record.last_synced_at = timezone.now()
        record.save(update_fields=["content_hash", "last_synced_at"])
        return False

    @staticmethod
    def _hash_payload(payload: dict[str, Any]) -> str:
        import json

        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ---------- check ----------

    def _handle_check(self) -> None:
        any_errors = False
        for app_config in apps.get_app_configs():
            path = self._resolve_schema_path(app_config)
            if path is None:
                continue
            text = path.read_text(encoding="utf-8")
            try:
                schema = parse_zed(text)
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"{app_config.name}: {exc}"))
                any_errors = True
                continue
            errors = validate_schema(schema)
            for e in errors:
                self.stderr.write(self.style.ERROR(f"  {app_config.name}: {e}"))
            if errors:
                any_errors = True
        if any_errors:
            raise CommandError("Schema check failed.")
        self.stdout.write(self.style.SUCCESS("OK"))

    # ---------- build-zed ----------

    def _handle_build_zed(self, options: dict[str, Any]) -> None:
        """Emit a byte-deterministic ``effective.zed``.

        Every package's ``permissions.zed`` is parsed to AST. Definitions,
        relations, permissions, caveats and allowed-subject unions are sorted
        alphabetically. Permission expressions are re-emitted with explicit
        parentheses around every binary op (per CLAUDE.md § 7). Source
        comments and ``// @rebac_*`` header metadata are dropped — they are
        not part of the schema. The header records a content hash computed
        over the body so any drift surfaces immediately.

        Per CLAUDE.md § 6 / docs/ARCHITECTURE.md § Determinism:
        no timestamps, no random, no insertion-order dependence.
        """
        from ... import __version__
        from ...conf import app_settings as conf_app_settings

        out_path = options.get("out")
        if out_path is None:
            base_dir = conf_app_settings.REBAC_SCHEMA_DIR or Path.cwd() / "rebac"
            base_dir = Path(base_dir)
            base_dir.mkdir(parents=True, exist_ok=True)
            out_path = base_dir / "effective.zed"
        else:
            out_path = Path(out_path)

        # Collect AST from every package, dropping per-source comments / headers.
        all_definitions: list[Any] = []
        all_caveats: list[Any] = []
        seen_definition_types: set[str] = set()
        seen_caveat_names: set[str] = set()
        for app_config in sorted(apps.get_app_configs(), key=lambda a: a.name):
            path = self._resolve_schema_path(app_config)
            if path is None:
                continue
            schema = parse_zed(path.read_text(encoding="utf-8"))
            for d in schema.definitions:
                if d.resource_type in seen_definition_types:
                    raise CommandError(
                        f"Duplicate definition {d.resource_type!r} found in {app_config.name}"
                    )
                seen_definition_types.add(d.resource_type)
                all_definitions.append(d)
            for c in schema.caveats:
                if c.name in seen_caveat_names:
                    raise CommandError(f"Duplicate caveat {c.name!r} found in {app_config.name}")
                seen_caveat_names.add(c.name)
                all_caveats.append(c)

        body = self._render_zed_body(
            sorted(all_definitions, key=lambda d: d.resource_type),
            sorted(all_caveats, key=lambda c: c.name),
        )
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        # Three-line header in fixed order; trailing newline guaranteed by `body`.
        output = (
            f"// Generated by django-zed-rebac {__version__}\n"
            f"// content_hash: {content_hash}\n"
            f"use typechecking\n"
            f"{body}"
        )
        # Force LF line endings unconditionally — `write_text` with `newline=""`
        # disables the platform-default translation.
        out_path.write_text(output, encoding="utf-8", newline="")
        self.stdout.write(self.style.SUCCESS(f"Wrote {out_path}"))

    # ----- deterministic emitter helpers -----

    def _render_zed_body(self, definitions: list[Any], caveats: list[Any]) -> str:
        """Render the post-header body. Always ends in a trailing newline."""
        parts: list[str] = []
        # Caveats first — definitions may reference them, mirrors sync order
        # and matches the SpiceDB convention.
        for c in caveats:
            parts.append(self._render_caveat(c))
        for d in definitions:
            parts.append(self._render_definition(d))
        # `\n` separates each block; trailing `\n` ensures POSIX-clean file.
        return ("\n" + "\n".join(parts)) if parts else "\n"

    def _render_caveat(self, c: Any) -> str:
        params = ", ".join(f"{p.name} {p.type}" for p in c.params)
        return f"caveat {c.name}({params}) {{\n{c.expression}\n}}\n"

    def _render_definition(self, d: Any) -> str:
        lines: list[str] = [f"definition {d.resource_type} {{"]
        relations = sorted(d.relations, key=lambda r: r.name)
        permissions = sorted(d.permissions, key=lambda p: p.name)
        for r in relations:
            lines.append(f"    {self._render_relation(r)}")
        if relations and permissions:
            lines.append("")
        for p in permissions:
            lines.append(f"    permission {p.name} = {self._render_expr(p.expression)}")
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _render_relation(self, r: Any) -> str:
        # Sort the type-union deterministically. Keys cover every distinguishing
        # field of `AllowedSubject` so equal-by-type subjects with different
        # subject-relations / wildcard / caveat / specific-id slots stay
        # distinguishable. `id` MUST appear in the key or two subjects that
        # differ only in id collapse to the same sort bucket — that breaks
        # CLAUDE.md § 6 byte-for-byte determinism on the universal-admin
        # pattern (`angee/role:admin#member` vs `angee/role:editor#member`).
        subjects = sorted(
            r.allowed_subjects,
            key=lambda s: (s.type, s.id, s.relation, s.wildcard, s.with_caveat),
        )
        rendered = " | ".join(self._render_subject(s) for s in subjects)
        suffix = " with expiration" if r.with_expiration else ""
        return f"relation {r.name}: {rendered}{suffix}"

    def _render_subject(self, s: Any) -> str:
        # Five shapes; specific-id forms (`type:id` / `type:id#relation`) are
        # the universal-admin pattern and were absent from earlier emitter
        # versions — dropping `id` here widened a single-role type union to
        # "members of any role of this type" and broke SpiceDB round-trip.
        if s.wildcard:
            base = f"{s.type}:*"
        elif s.id and s.relation:
            base = f"{s.type}:{s.id}#{s.relation}"
        elif s.id:
            base = f"{s.type}:{s.id}"
        elif s.relation:
            base = f"{s.type}#{s.relation}"
        else:
            base = s.type
        if s.with_caveat:
            base += f" with {s.with_caveat}"
        return base

    def _render_expr(self, expr: Any) -> str:
        # Lazy import — keep AST coupling local to this method.
        from ...schema.ast import PermArrow, PermBinOp, PermNil, PermRef

        if isinstance(expr, PermNil):
            return "nil"
        if isinstance(expr, PermRef):
            return expr.name
        if isinstance(expr, PermArrow):
            return f"{expr.via}->{expr.target}"
        if isinstance(expr, PermBinOp):
            # Per CLAUDE.md § 7: always parenthesise compound expressions.
            # Operand order is preserved — `+` and `&` are commutative but
            # reordering them changes meaning when arrows / caveats are in
            # play (sorted definitions / relations is enough for determinism).
            left = self._render_expr(expr.left)
            right = self._render_expr(expr.right)
            return f"({left} {expr.op} {right})"
        raise CommandError(f"Unknown expression node: {type(expr).__name__}")

    # ---------- migrate-storage (proposal 0001) ----------

    def _handle_migrate_storage(self, options: dict[str, Any]) -> None:
        """Copy rows between ``Relationship`` and ``RelationshipRegistry``.

        Both directions supported. Source defaults to the opposite of
        ``--to``. The active table per
        ``REBAC_LOCAL_BACKEND_STORAGE`` is not changed by this command —
        the operator flips the setting after the copy completes.

        Implementation notes:

        - The denormalized → registry path upserts ``RebacResource`` rows
          in bulk (``upsert_refs_bulk``) per batch, then issues a single
          ``bulk_create(ignore_conflicts=True)`` for the registry rows.
        - The registry → denormalized path projects through the FK rows
          and writes denormalized rows in a single ``bulk_create``.
        - Row-count parity is verified at the end; mismatch raises.
        - ``--dry-run`` short-circuits before writes so consumers can
          gauge cost.
        - The source table is never dropped — the operator confirms by
          flipping the setting then drops manually.
        """

        from ...conf import app_settings
        from ...models import RebacResource, Relationship, RelationshipRegistry

        dst = options["dst"]
        src = options["src"] or ("registry" if dst == "denormalized" else "denormalized")
        if src == dst:
            raise CommandError("--from and --to must differ")
        batch = options["batch"] or app_settings.REBAC_LOCAL_BACKEND_REGISTRY_BATCH_SIZE
        dry_run = options["dry_run"]

        if src == "denormalized":
            src_count = Relationship.objects.count()
        else:
            src_count = RelationshipRegistry.objects.count()
        dst_count_before = (
            RelationshipRegistry.objects.count()
            if dst == "registry"
            else Relationship.objects.count()
        )

        self.stdout.write(
            f"migrate-storage {src} -> {dst}: src={src_count} dst_before={dst_count_before}"
        )
        if dry_run:
            self.stdout.write("--dry-run: no writes performed")
            return
        if src_count == 0:
            self.stdout.write("source table empty; nothing to copy")
            return

        if src == "denormalized" and dst == "registry":
            self._copy_to_registry(Relationship, RelationshipRegistry, RebacResource, batch)
        else:
            self._copy_to_denormalized(RelationshipRegistry, Relationship, batch)

        dst_count_after = (
            RelationshipRegistry.objects.count()
            if dst == "registry"
            else Relationship.objects.count()
        )
        added = dst_count_after - dst_count_before
        self.stdout.write(f"migrate-storage done: dst_after={dst_count_after} (+{added})")
        # Parity check — destinations may already hold some rows
        # (re-running a partial migration is supported). The total
        # destination row count after the copy must cover the unique-key
        # tuples that exist in the source: every source row's
        # unique-key signature must have a counterpart in the destination.
        # Anything else is a silent dedup (registry unique-constraint
        # collapse, write failure absorbed by ignore_conflicts) and
        # warrants an abort.
        expected_after = max(dst_count_before, src_count)
        if dst_count_after < expected_after:
            raise CommandError(
                f"parity: destination has {dst_count_after} rows but expected at "
                f"least {expected_after} (src={src_count}, dst_before={dst_count_before}). "
                f"Investigate dedup or ignored write failures."
            )
        if added > src_count:
            raise CommandError(
                f"parity: destination grew by {added} but source had only {src_count} rows. "
                f"Concurrent writes during migration?"
            )

    def _copy_to_registry(
        self,
        src_model: type,
        dst_model: type,
        resource_model: type,
        batch: int,
    ) -> None:
        """Stream rows from the denormalized table into the registry one.

        For each batch:
          1. Collect the unique ``(type, id)`` pairs across resource and
             subject sides.
          2. ``upsert_refs_bulk`` returns the ``(type, id) → pk`` map.
          3. Build registry rows with ``resource_fk_id`` / ``subject_fk_id``
             populated from the map.
          4. ``bulk_create(ignore_conflicts=True)`` writes them; reruns
             are idempotent.
        """
        from django.db import transaction

        total = 0
        qs = src_model.objects.all().order_by("pk")
        offset = 0
        while True:
            rows = list(qs[offset : offset + batch])
            if not rows:
                break
            pairs: list[tuple[str, str]] = []
            for r in rows:
                pairs.append((r.resource_type, r.resource_id))
                pairs.append((r.subject_type, r.subject_id))
            with transaction.atomic():
                pk_map = resource_model.upsert_refs_bulk(pairs)
                new_rows = [
                    dst_model(
                        resource_fk_id=pk_map[(r.resource_type, r.resource_id)],
                        relation=r.relation,
                        subject_fk_id=pk_map[(r.subject_type, r.subject_id)],
                        optional_subject_relation=r.optional_subject_relation,
                        caveat_name=r.caveat_name,
                        caveat_context=r.caveat_context,
                        expires_at=r.expires_at,
                        written_at_xid=r.written_at_xid,
                    )
                    for r in rows
                ]
                dst_model.objects.bulk_create(new_rows, ignore_conflicts=True)
            total += len(rows)
            offset += batch
            self.stdout.write(f"  copied {total}/...")
        self.stdout.write(f"  total: {total} rows")

    def _copy_to_denormalized(
        self,
        src_model: type,
        dst_model: type,
        batch: int,
    ) -> None:
        """Stream rows from the registry table into the denormalized one.

        Projects through ``resource_fk`` / ``subject_fk`` to get the
        string columns. ``bulk_create(ignore_conflicts=True)`` deduplicates.
        """
        from django.db import transaction

        total = 0
        qs = src_model.objects.all().select_related("resource_fk", "subject_fk").order_by("pk")
        offset = 0
        while True:
            rows = list(qs[offset : offset + batch])
            if not rows:
                break
            with transaction.atomic():
                new_rows = [
                    dst_model(
                        resource_type=r.resource_fk.resource_type,
                        resource_id=r.resource_fk.resource_id,
                        relation=r.relation,
                        subject_type=r.subject_fk.resource_type,
                        subject_id=r.subject_fk.resource_id,
                        optional_subject_relation=r.optional_subject_relation,
                        caveat_name=r.caveat_name,
                        caveat_context=r.caveat_context,
                        expires_at=r.expires_at,
                        written_at_xid=r.written_at_xid,
                    )
                    for r in rows
                ]
                dst_model.objects.bulk_create(new_rows, ignore_conflicts=True)
            total += len(rows)
            offset += batch
            self.stdout.write(f"  copied {total}/...")
        self.stdout.write(f"  total: {total} rows")

    # ---------- explain ----------

    def _handle_explain(self, options: dict[str, Any]) -> None:
        target = options["target"]
        if "." not in target:
            raise CommandError("explain target must be <type>.<perm>")
        rt, perm = target.rsplit(".", 1)
        from ...models import SchemaDefinition

        try:
            d = SchemaDefinition.objects.get(resource_type=rt)
        except SchemaDefinition.DoesNotExist as exc:
            raise CommandError(f"No definition: {rt}") from exc
        try:
            p = d.permissions.get(name=perm)
        except Exception as exc:
            raise CommandError(f"No permission {perm!r} on {rt}: {exc}") from exc
        self.stdout.write(f"{rt}#{perm} = {p.expression}")
