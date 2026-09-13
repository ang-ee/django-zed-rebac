# Proposal 0006: model-owned subject identity

Status: accepted for implementation.

## Problem

`RebacMixin` models already own their REBAC object type and identifier through
`Meta.rebac_resource_type` and `Meta.rebac_id_attr`. `to_object_ref()` consumes
that declaration, model lookup uses it, and field-backed relations use the same
identifier. `to_subject_ref()` instead recognizes Django's configured user and
contrib group classes specially, then falls back to the separate
`@rebac_subject` registry.

That split forces a Django model which is both a resource and a subject to
declare the same type and identifier twice. It also cannot express the usual
subject-set form of a container, such as `auth/group:<id>#member`, through model
metadata.

## Contract

A Django model with `Meta.rebac_resource_type` is directly convertible to a
`SubjectRef`. Its subject type and identifier are exactly its `ObjectRef` type
and identifier. It may additionally declare:

```python
class Meta:
    rebac_resource_type = "auth/group"
    rebac_id_attr = "pk"
    rebac_subject_relation = "member"
```

`to_subject_ref(instance)` then returns the instance's object reference as a
subject, with `rebac_subject_relation` as its optional relation. Omitting the
option produces a concrete subject with no optional relation.

Explicit model metadata takes precedence over built-in User and Group type
settings. The configured user still must be a saved, authenticated instance.
Models without explicit REBAC metadata retain the existing configured User and
contrib Group fallbacks. `@rebac_subject` remains the registration surface for
plain Python subject objects; Django models do not need a parallel registration.
Every generated subject type follows `REBAC_TYPE_PREFIX`, matching generated object
identity. An already canonical `SubjectRef` remains unchanged.

Model lookup for field-backed relation targets follows the same precedence: a
loaded model declaring the requested resource type wins, with configured
User/Group mappings serving only as fallbacks.

## Validation

The system-check owner reports a model whose non-empty
`rebac_subject_relation` is absent from its effective schema definition as
`rebac.E011`. Runtime conversion remains a pure metadata operation and performs
no schema or database lookup. The inverse mapping — subject type to Django
model and id attribute — has one owner, `rebac.resources.model_for_subject_type`,
shared by backing resolution and `resolve_subjects`.

## Compatibility

The new metadata is optional. Existing Django User and contrib Group conversion
keeps its historical wire form. Existing `@rebac_subject` classes are unchanged.
Projects can adopt model-owned identity one model at a time.
