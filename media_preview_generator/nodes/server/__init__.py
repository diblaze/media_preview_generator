"""The node server: what runs ON a node (``MODE=node``). Never imported by the app's web process.

Entry point: :func:`media_preview_generator.nodes.server.app.create_node_app` (gunicorn: ``nodes.server.wsgi:app``).
"""
