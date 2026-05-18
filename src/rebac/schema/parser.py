"""Parser for SpiceDB-native .zed files.

Hand-written recursive-descent. Covers the SpiceDB-canonical subset relevant
to Django projects (per docs/ZED.md § Reference): definitions, relations with
type unions and subject sets, permissions with `+`/`&`/`-`/arrows, caveats,
wildcards, `use expiration`, `use typechecking`. Does NOT cover composable
schemas (`use import`), `use self`, or the `nil` shortcut beyond keyword.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ast import (
    BUILTIN_ACTOR_TYPES,
    AllowedSubject,
    Caveat,
    CaveatParam,
    Definition,
    PermArrow,
    PermBinOp,
    PermExpr,
    Permission,
    PermNil,
    PermRef,
    Relation,
    Schema,
)

_HEADER_RE = re.compile(r"^//\s*@(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<value>.+?)\s*$")
_TYPE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ParseError(Exception):
    pass


# ---------- Tokenizer ----------


@dataclass
class Token:
    kind: str  # "ident", "type", "punct", "keyword", "string", "eof"
    value: str
    line: int
    col: int


_KEYWORDS = {"definition", "relation", "permission", "caveat", "use", "with", "nil"}
# Punct includes characters that legally appear inside caveat CEL bodies so
# the tokenizer can sweep past them; the caveat parser re-reads the body
# from raw source via offset scanning.
_PUNCT = set("{}|+&-=:>,()*#.<[];!?")


def _tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    line, col = 1, 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            i += 1
            line += 1
            col = 1
            continue
        if c.isspace():
            i += 1
            col += 1
            continue
        # Line comment
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        # Block comment
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                raise ParseError(f"Unterminated block comment at line {line}")
            for ch in text[i : j + 2]:
                if ch == "\n":
                    line += 1
                    col = 1
                else:
                    col += 1
            i = j + 2
            continue
        # Identifiers / type names / keywords
        if c.isalpha() or c == "_":
            m = _TYPE_NAME_RE.match(text, i)
            assert m
            value = m.group(0)
            kind = "keyword" if value in _KEYWORDS else ("type" if "/" in value else "ident")
            tokens.append(Token(kind, value, line, col))
            consumed = len(value)
            i += consumed
            col += consumed
            continue
        # Multi-char punct: "->"
        if c == "-" and i + 1 < n and text[i + 1] == ">":
            tokens.append(Token("punct", "->", line, col))
            i += 2
            col += 2
            continue
        # Single-char punct
        if c in _PUNCT:
            tokens.append(Token("punct", c, line, col))
            i += 1
            col += 1
            continue
        # String literals (used in caveat expressions if they appear inline — uncommon)
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 2
                else:
                    j += 1
            if j >= n:
                raise ParseError(f"Unterminated string at line {line}")
            tokens.append(Token("string", text[i + 1 : j], line, col))
            i = j + 1
            col += j + 1 - i + len(text[i + 1 : j])
            continue
        raise ParseError(f"Unexpected character {c!r} at line {line}, col {col}")
    tokens.append(Token("eof", "", line, col))
    return tokens


# ---------- Parser ----------


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0

    # ----- helpers -----
    def peek(self, offset: int = 0) -> Token:
        idx = self.pos + offset
        if idx >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[idx]

    def consume(self) -> Token:
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect(self, kind: str, value: str | None = None) -> Token:
        t = self.consume()
        if t.kind != kind or (value is not None and t.value != value):
            want = f"{kind}={value!r}" if value else kind
            raise ParseError(
                f"Expected {want} at line {t.line}, col {t.col}; got {t.kind}={t.value!r}"
            )
        return t

    def at(self, kind: str, value: str | None = None) -> bool:
        t = self.peek()
        return t.kind == kind and (value is None or t.value == value)

    # ----- top-level -----
    def parse(self) -> Schema:
        schema = Schema(headers=self._extract_headers())
        while not self.at("eof"):
            if self.at("keyword", "use"):
                self.consume()
                pieces: list[str] = ["use"]
                while not self.at("eof") and not (
                    self.at("keyword", "definition")
                    or self.at("keyword", "caveat")
                    or self.at("keyword", "use")
                ):
                    pieces.append(self.consume().value)
                schema.directives.append(" ".join(pieces))
            elif self.at("keyword", "definition"):
                schema.definitions.append(self._parse_definition())
            elif self.at("keyword", "caveat"):
                schema.caveats.append(self._parse_caveat())
            else:
                t = self.peek()
                raise ParseError(f"Unexpected token at line {t.line}, col {t.col}: {t.value!r}")
        return schema

    def parse_permission_expression(self) -> PermExpr:
        """Parse only a permission expression from the token stream."""
        expr = self._parse_expr_exclusion()
        if not self.at("eof"):
            t = self.peek()
            raise ParseError(f"Unexpected token at line {t.line}, col {t.col}: {t.value!r}")
        return expr

    def _extract_headers(self) -> dict[str, str]:
        # Re-scan source for `// @key: value` header comments — tokenizer drops comments.
        headers: dict[str, str] = {}
        for line in self.text.splitlines():
            m = _HEADER_RE.match(line.strip())
            if m:
                headers[m.group("key")] = m.group("value")
        return headers

    # ----- definition -----
    def _parse_definition(self) -> Definition:
        self.expect("keyword", "definition")
        type_tok = self.consume()
        if type_tok.kind not in ("type", "ident"):
            raise ParseError(
                f"Expected definition type name at line {type_tok.line}; got {type_tok.value!r}"
            )
        resource_type = type_tok.value
        self.expect("punct", "{")
        relations: list[Relation] = []
        permissions: list[Permission] = []
        while not self.at("punct", "}"):
            if self.at("keyword", "relation"):
                relations.append(self._parse_relation())
            elif self.at("keyword", "permission"):
                permissions.append(self._parse_permission())
            else:
                t = self.peek()
                raise ParseError(f"Expected relation/permission at line {t.line}; got {t.value!r}")
        self.expect("punct", "}")
        return Definition(resource_type, tuple(relations), tuple(permissions))

    def _parse_relation(self) -> Relation:
        self.expect("keyword", "relation")
        name = self.expect("ident").value
        self.expect("punct", ":")
        subjects = self._parse_subject_union()
        with_expiration = False
        if self.at("keyword", "with"):
            self.consume()
            ident = self.consume()
            if ident.value != "expiration":
                raise ParseError(
                    f"Expected 'expiration' after 'with' at line {ident.line}; got {ident.value!r}"
                )
            with_expiration = True
        return Relation(name, tuple(subjects), with_expiration)

    def _parse_subject_union(self) -> list[AllowedSubject]:
        subjects = [self._parse_subject_term()]
        while self.at("punct", "|"):
            self.consume()
            subjects.append(self._parse_subject_term())
        return subjects

    def _parse_subject_term(self) -> AllowedSubject:
        type_tok = self.consume()
        if type_tok.kind not in ("type", "ident"):
            raise ParseError(f"Expected subject type at line {type_tok.line}")
        type_name = type_tok.value
        relation = ""
        wildcard = False
        with_caveat = ""
        specific_id = ""
        if self.at("punct", ":"):
            # Three shapes after `:`
            #   `auth/user:*`                  → wildcard
            #   `angee/role:admin`             → specific id, no subject-set
            #   `angee/role:admin#member`      → specific id + subject-set
            self.consume()
            next_tok = self.consume()
            if next_tok.kind == "punct" and next_tok.value == "*":
                wildcard = True
            elif next_tok.kind == "ident":
                # Schema-side specific-ids are constrained to identifier shape
                # — `[A-Za-z_][A-Za-z0-9_]*`. See `AllowedSubject.id`. The
                # tokenizer never emits an `ident` that violates this, so the
                # parser side has nothing extra to check; bare-`type` tokens
                # (containing `/`) are rejected below because a specific id
                # cannot contain a namespace separator.
                specific_id = next_tok.value
                if self.at("punct", "#"):
                    self.consume()
                    relation = self.expect("ident").value
            else:
                # Common authoring mistakes: `role:42` (digit-start, fails at
                # tokenizer level — caller never reaches here); `role:obj-admin`
                # (hyphen splits at tokenizer); `auth/sub/type:something`
                # (namespace separator in id, lands here as kind="type").
                raise ParseError(
                    f"Expected '*' or identifier-shaped resource id after ':' in "
                    f"subject term at line {next_tok.line}; got "
                    f"{next_tok.kind}={next_tok.value!r}. Specific ids in type "
                    f"unions must match [A-Za-z_][A-Za-z0-9_]*."
                )
        elif self.at("punct", "#"):
            self.consume()
            relation = self.expect("ident").value
        if self.at("keyword", "with"):
            # `... with caveat_name`
            self.consume()
            with_caveat = self.expect("ident").value
        return AllowedSubject(type_name, relation, wildcard, with_caveat, specific_id)

    def _parse_permission(self) -> Permission:
        self.expect("keyword", "permission")
        name = self.expect("ident").value
        self.expect("punct", "=")
        # Capture raw text: re-stitch tokens until end of expression
        start = self.pos
        expr = self._parse_expr_exclusion()
        # Reconstruct raw text from consumed tokens — best-effort.
        raw_pieces: list[str] = []
        for tok in self.tokens[start : self.pos]:
            raw_pieces.append(tok.value)
        return Permission(name, expr, " ".join(raw_pieces))

    # Expression parsing — operator precedence: + (tightest) < & < -
    # (left-associative, per SpiceDB).
    def _parse_expr_exclusion(self) -> PermExpr:
        left = self._parse_expr_intersection()
        while self.at("punct", "-"):
            self.consume()
            right = self._parse_expr_intersection()
            left = PermBinOp("-", left, right)
        return left

    def _parse_expr_intersection(self) -> PermExpr:
        left = self._parse_expr_union()
        while self.at("punct", "&"):
            self.consume()
            right = self._parse_expr_union()
            left = PermBinOp("&", left, right)
        return left

    def _parse_expr_union(self) -> PermExpr:
        left = self._parse_expr_atom()
        while self.at("punct", "+"):
            self.consume()
            right = self._parse_expr_atom()
            left = PermBinOp("+", left, right)
        return left

    def _parse_expr_atom(self) -> PermExpr:
        if self.at("punct", "("):
            self.consume()
            inner = self._parse_expr_exclusion()
            self.expect("punct", ")")
            return inner
        if self.at("keyword", "nil"):
            self.consume()
            return PermNil()
        # ident or type — first token of arrow / ref
        t = self.consume()
        if t.kind not in ("ident", "type"):
            raise ParseError(
                f"Expected identifier or '(' in expression at line {t.line}; got {t.value!r}"
            )
        # arrow?
        if self.at("punct", "->"):
            self.consume()
            target = self.expect("ident").value
            return PermArrow(t.value, target)
        return PermRef(t.value)

    # ----- caveat -----
    def _parse_caveat(self) -> Caveat:
        self.expect("keyword", "caveat")
        name = self.expect("ident").value
        self.expect("punct", "(")
        params: list[CaveatParam] = []
        if not self.at("punct", ")"):
            params.append(self._parse_caveat_param())
            while self.at("punct", ","):
                self.consume()
                params.append(self._parse_caveat_param())
        self.expect("punct", ")")
        # Caveat body is CEL — too rich to tokenize. Find its source span by
        # scanning the original text from the position of the opening `{`.
        open_brace = self.peek()
        if not (open_brace.kind == "punct" and open_brace.value == "{"):
            raise ParseError(
                f"Expected '{{' to open caveat body at line {open_brace.line}; "
                f"got {open_brace.value!r}"
            )
        body_start = self._absolute_offset_of(open_brace)
        body_text, body_end = self._scan_braced_block(body_start)
        # Re-synchronise the token stream past the closing `}`.
        self._advance_past_offset(body_end)
        return Caveat(name, tuple(params), body_text.strip())

    # ----- helpers used by caveat body scanning -----

    def _absolute_offset_of(self, tok: Token) -> int:
        """Compute byte offset of `tok` in the source by walking line/col."""
        # Simpler: rebuild from line indices.
        offset = 0
        line_no = 1
        for line in self.text.splitlines(keepends=True):
            if line_no == tok.line:
                return offset + tok.col - 1
            offset += len(line)
            line_no += 1
        return offset

    def _scan_braced_block(self, start_offset: int) -> tuple[str, int]:
        """Scan from `start_offset` (positioned on `{`) to matching `}`.

        Returns (body_text_without_braces, end_offset_after_close_brace).
        Honours string literals so `}` inside a quoted string isn't a close.
        """
        if self.text[start_offset] != "{":
            raise ParseError(f"expected '{{' at offset {start_offset}")
        i = start_offset + 1
        body_start = i
        depth = 1
        n = len(self.text)
        while i < n:
            c = self.text[i]
            if c == '"':
                # consume string
                i += 1
                while i < n and self.text[i] != '"':
                    if self.text[i] == "\\":
                        i += 2
                    else:
                        i += 1
                i += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return self.text[body_start:i], i + 1
            i += 1
        raise ParseError("Unterminated caveat body")

    def _advance_past_offset(self, target_offset: int) -> None:
        """Move the token cursor to the first token whose start offset >= target."""
        for idx, tok in enumerate(self.tokens[self.pos :], start=self.pos):
            if tok.kind == "eof":
                self.pos = idx
                return
            if self._absolute_offset_of(tok) >= target_offset:
                self.pos = idx
                return
        self.pos = len(self.tokens) - 1

    def _parse_caveat_param(self) -> CaveatParam:
        name = self.expect("ident").value
        # Type may be a simple ident like `int`, `string`, `ipaddress`, `timestamp`,
        # or a generic like `list<string>` — accept any sequence of idents/punct
        # until comma or close paren.
        type_pieces: list[str] = []
        while not self.at("eof") and not self.at("punct", ",") and not self.at("punct", ")"):
            type_pieces.append(self.consume().value)
        if not type_pieces:
            t = self.peek()
            raise ParseError(f"Expected caveat parameter type at line {t.line}")
        return CaveatParam(name, " ".join(type_pieces).strip())


def parse_zed(text: str) -> Schema:
    """Parse a .zed file into a Schema AST."""
    return _Parser(text).parse()


def parse_permission_expression(text: str) -> PermExpr:
    """Public API to parse a single permission expression."""
    return _Parser(text).parse_permission_expression()


def validate_schema(schema: Schema) -> list[str]:
    """Cross-check references inside the schema. Returns a list of error strings.

    Empty list means valid.
    """
    errors: list[str] = []
    caveat_names = {c.name for c in schema.caveats}

    for definition in schema.definitions:
        if definition.resource_type in BUILTIN_ACTOR_TYPES:
            errors.append(
                f"{definition.resource_type!r} is a reserved built-in actor "
                "and cannot be declared as a definition"
            )
        relation_names = {r.name for r in definition.relations}
        permission_names = {p.name for p in definition.permissions}

        for relation in definition.relations:
            if relation.name in BUILTIN_ACTOR_TYPES:
                errors.append(
                    f"{definition.resource_type}#{relation.name}: "
                    "reserved built-in actors cannot be declared as relations"
                )
            for sub in relation.allowed_subjects:
                if sub.type in BUILTIN_ACTOR_TYPES:
                    errors.append(
                        f"{definition.resource_type}#{relation.name}: "
                        f"reserved built-in actor {sub.type!r} is valid only "
                        "inside permission expressions"
                    )
                # Cross-package references are validated at sync time, not here —
                # we don't know what other packages are loaded. We DO check that
                # caveat names referenced inline exist (when present in the same file).
                if sub.with_caveat and sub.with_caveat not in caveat_names:
                    errors.append(
                        f"{definition.resource_type}#{relation.name}: "
                        f"unknown caveat {sub.with_caveat!r}"
                    )

        for perm in definition.permissions:
            errors.extend(
                _validate_expr(perm.expression, definition, relation_names, permission_names)
            )
        # Detect duplicate names within a definition.
        all_names = list(relation_names) + list(permission_names)
        if len(all_names) != len(set(all_names)):
            errors.append(
                f"{definition.resource_type}: name collision between relations and permissions"
            )

    return errors


def _validate_expr(
    expr: PermExpr,
    definition: Definition,
    relation_names: set[str],
    permission_names: set[str],
) -> list[str]:
    errors: list[str] = []
    if isinstance(expr, PermNil):
        return errors
    if isinstance(expr, PermRef):
        if expr.name in BUILTIN_ACTOR_TYPES:
            return errors
        if expr.name not in relation_names and expr.name not in permission_names:
            errors.append(
                f"{definition.resource_type}: undefined reference {expr.name!r} in expression"
            )
        return errors
    if isinstance(expr, PermArrow):
        if expr.via not in relation_names:
            errors.append(
                f"{definition.resource_type}: arrow walks via undefined relation {expr.via!r}"
            )
        return errors
    if isinstance(expr, PermBinOp):
        errors.extend(_validate_expr(expr.left, definition, relation_names, permission_names))
        errors.extend(_validate_expr(expr.right, definition, relation_names, permission_names))
    return errors
