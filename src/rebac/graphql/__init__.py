"""GraphQL adapters for ``django-zed-rebac``.

Currently ships the Strawberry/Channels adapter under
:mod:`rebac.graphql.strawberry` and the Strawberry-Django optimizer under
:mod:`rebac.graphql.strawberry_django`. Behind the ``[strawberry]`` /
``[strawberry-django]`` extras — importing either module without its optional
dependency installed raises a plain ``ImportError`` naming the missing package.

Future Graphene / Ariadne adapters would land alongside as
``rebac.graphql.graphene`` / ``rebac.graphql.ariadne``; same
extras-driven pattern.
"""
