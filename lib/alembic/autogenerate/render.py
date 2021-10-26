from collections import OrderedDict
from io import StringIO
import re
from typing import Any
from typing import cast
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import TYPE_CHECKING
from typing import Union

from mako.pygen import PythonPrinter
from sqlalchemy import schema as sa_schema
from sqlalchemy import sql
from sqlalchemy import types as sqltypes
from sqlalchemy.sql.elements import conv

from .. import util
from ..operations import ops
from ..util import compat
from ..util import sqla_compat
from ..util.compat import string_types

if TYPE_CHECKING:
    from typing import Literal

    from sqlalchemy.sql.elements import ColumnElement
    from sqlalchemy.sql.elements import quoted_name
    from sqlalchemy.sql.elements import TextClause
    from sqlalchemy.sql.schema import CheckConstraint
    from sqlalchemy.sql.schema import Column
    from sqlalchemy.sql.schema import Constraint
    from sqlalchemy.sql.schema import DefaultClause
    from sqlalchemy.sql.schema import FetchedValue
    from sqlalchemy.sql.schema import ForeignKey
    from sqlalchemy.sql.schema import ForeignKeyConstraint
    from sqlalchemy.sql.schema import Index
    from sqlalchemy.sql.schema import MetaData
    from sqlalchemy.sql.schema import PrimaryKeyConstraint
    from sqlalchemy.sql.schema import UniqueConstraint
    from sqlalchemy.sql.sqltypes import ARRAY
    from sqlalchemy.sql.type_api import TypeEngine
    from sqlalchemy.sql.type_api import Variant

    from alembic.autogenerate.api import AutogenContext
    from alembic.config import Config
    from alembic.operations.ops import MigrationScript
    from alembic.operations.ops import ModifyTableOps
    from alembic.util.sqla_compat import Computed
    from alembic.util.sqla_compat import Identity


MAX_PYTHON_ARGS = 255


def _render_gen_name(
    autogen_context: "AutogenContext",
    name: Optional[Union["quoted_name", str]],
) -> Optional[Union["quoted_name", str, "_f_name"]]:
    if isinstance(name, conv):
        return _f_name(_alembic_autogenerate_prefix(autogen_context), name)
    else:
        return name


def _indent(text: str) -> str:
    text = re.compile(r"^", re.M).sub("    ", text).strip()
    text = re.compile(r" +$", re.M).sub("", text)
    return text


def _render_python_into_templatevars(
    autogen_context: "AutogenContext",
    migration_script: "MigrationScript",
    template_args: Dict[str, Union[str, "Config"]],
) -> None:
    imports = autogen_context.imports

    for upgrade_ops, downgrade_ops in zip(
        migration_script.upgrade_ops_list, migration_script.downgrade_ops_list
    ):
        template_args[upgrade_ops.upgrade_token] = _indent(
            _render_cmd_body(upgrade_ops, autogen_context)
        )
        template_args[downgrade_ops.downgrade_token] = _indent(
            _render_cmd_body(downgrade_ops, autogen_context)
        )
    template_args["imports"] = "\n".join(sorted(imports))


default_renderers = renderers = util.Dispatcher()


def _render_cmd_body(
    op_container: "ops.OpContainer",
    autogen_context: "AutogenContext",
) -> str:

    buf = StringIO()
    printer = PythonPrinter(buf)

    printer.writeline(
        "# ### commands auto generated by Alembic - please adjust! ###"
    )

    has_lines = False
    for op in op_container.ops:
        lines = render_op(autogen_context, op)
        has_lines = has_lines or bool(lines)

        for line in lines:
            printer.writeline(line)

    if not has_lines:
        printer.writeline("pass")

    printer.writeline("# ### end Alembic commands ###")

    return buf.getvalue()


def render_op(
    autogen_context: "AutogenContext", op: "ops.MigrateOperation"
) -> List[str]:
    renderer = renderers.dispatch(op)
    lines = util.to_list(renderer(autogen_context, op))
    return lines


def render_op_text(
    autogen_context: "AutogenContext", op: "ops.MigrateOperation"
) -> str:
    return "\n".join(render_op(autogen_context, op))


@renderers.dispatch_for(ops.ModifyTableOps)
def _render_modify_table(
    autogen_context: "AutogenContext", op: "ModifyTableOps"
) -> List[str]:
    opts = autogen_context.opts
    render_as_batch = opts.get("render_as_batch", False)

    if op.ops:
        lines = []
        if render_as_batch:
            with autogen_context._within_batch():
                lines.append(
                    "with op.batch_alter_table(%r, schema=%r) as batch_op:"
                    % (op.table_name, op.schema)
                )
                for t_op in op.ops:
                    t_lines = render_op(autogen_context, t_op)
                    lines.extend(t_lines)
                lines.append("")
        else:
            for t_op in op.ops:
                t_lines = render_op(autogen_context, t_op)
                lines.extend(t_lines)

        return lines
    else:
        return []


@renderers.dispatch_for(ops.CreateTableCommentOp)
def _render_create_table_comment(
    autogen_context: "AutogenContext", op: "ops.CreateTableCommentOp"
) -> str:

    templ = (
        "{prefix}create_table_comment(\n"
        "{indent}'{tname}',\n"
        "{indent}{comment},\n"
        "{indent}existing_comment={existing},\n"
        "{indent}schema={schema}\n"
        ")"
    )
    return templ.format(
        prefix=_alembic_autogenerate_prefix(autogen_context),
        tname=op.table_name,
        comment="%r" % op.comment if op.comment is not None else None,
        existing="%r" % op.existing_comment
        if op.existing_comment is not None
        else None,
        schema="'%s'" % op.schema if op.schema is not None else None,
        indent="    ",
    )


@renderers.dispatch_for(ops.DropTableCommentOp)
def _render_drop_table_comment(
    autogen_context: "AutogenContext", op: "ops.DropTableCommentOp"
) -> str:

    templ = (
        "{prefix}drop_table_comment(\n"
        "{indent}'{tname}',\n"
        "{indent}existing_comment={existing},\n"
        "{indent}schema={schema}\n"
        ")"
    )
    return templ.format(
        prefix=_alembic_autogenerate_prefix(autogen_context),
        tname=op.table_name,
        existing="%r" % op.existing_comment
        if op.existing_comment is not None
        else None,
        schema="'%s'" % op.schema if op.schema is not None else None,
        indent="    ",
    )


@renderers.dispatch_for(ops.CreateTableOp)
def _add_table(
    autogen_context: "AutogenContext", op: "ops.CreateTableOp"
) -> str:
    table = op.to_table()

    args = [
        col
        for col in [
            _render_column(col, autogen_context) for col in table.columns
        ]
        if col
    ] + sorted(
        [
            rcons
            for rcons in [
                _render_constraint(
                    cons, autogen_context, op._namespace_metadata
                )
                for cons in table.constraints
            ]
            if rcons is not None
        ]
    )

    if len(args) > MAX_PYTHON_ARGS:
        args_str = "*[" + ",\n".join(args) + "]"
    else:
        args_str = ",\n".join(args)

    text = "%(prefix)screate_table(%(tablename)r,\n%(args)s" % {
        "tablename": _ident(op.table_name),
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "args": args_str,
    }
    if op.schema:
        text += ",\nschema=%r" % _ident(op.schema)

    comment = table.comment
    if comment:
        text += ",\ncomment=%r" % _ident(comment)
    for k in sorted(op.kw):
        text += ",\n%s=%r" % (k.replace(" ", "_"), op.kw[k])

    if table._prefixes:
        prefixes = ", ".join("'%s'" % p for p in table._prefixes)
        text += ",\nprefixes=[%s]" % prefixes

    text += "\n)"
    return text


@renderers.dispatch_for(ops.DropTableOp)
def _drop_table(
    autogen_context: "AutogenContext", op: "ops.DropTableOp"
) -> str:
    text = "%(prefix)sdrop_table(%(tname)r" % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "tname": _ident(op.table_name),
    }
    if op.schema:
        text += ", schema=%r" % _ident(op.schema)
    text += ")"
    return text


@renderers.dispatch_for(ops.CreateIndexOp)
def _add_index(
    autogen_context: "AutogenContext", op: "ops.CreateIndexOp"
) -> str:
    index = op.to_index()

    has_batch = autogen_context._has_batch

    if has_batch:
        tmpl = (
            "%(prefix)screate_index(%(name)r, [%(columns)s], "
            "unique=%(unique)r%(kwargs)s)"
        )
    else:
        tmpl = (
            "%(prefix)screate_index(%(name)r, %(table)r, [%(columns)s], "
            "unique=%(unique)r%(schema)s%(kwargs)s)"
        )

    assert index.table is not None
    text = tmpl % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "name": _render_gen_name(autogen_context, index.name),
        "table": _ident(index.table.name),
        "columns": ", ".join(
            _get_index_rendered_expressions(index, autogen_context)
        ),
        "unique": index.unique or False,
        "schema": (", schema=%r" % _ident(index.table.schema))
        if index.table.schema
        else "",
        "kwargs": (
            ", "
            + ", ".join(
                [
                    "%s=%s"
                    % (key, _render_potential_expr(val, autogen_context))
                    for key, val in index.kwargs.items()
                ]
            )
        )
        if len(index.kwargs)
        else "",
    }
    return text


@renderers.dispatch_for(ops.DropIndexOp)
def _drop_index(
    autogen_context: "AutogenContext", op: "ops.DropIndexOp"
) -> str:
    index = op.to_index()

    has_batch = autogen_context._has_batch

    if has_batch:
        tmpl = "%(prefix)sdrop_index(%(name)r%(kwargs)s)"
    else:
        tmpl = (
            "%(prefix)sdrop_index(%(name)r, "
            "table_name=%(table_name)r%(schema)s%(kwargs)s)"
        )

    text = tmpl % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "name": _render_gen_name(autogen_context, op.index_name),
        "table_name": _ident(op.table_name),
        "schema": ((", schema=%r" % _ident(op.schema)) if op.schema else ""),
        "kwargs": (
            ", "
            + ", ".join(
                [
                    "%s=%s"
                    % (key, _render_potential_expr(val, autogen_context))
                    for key, val in index.kwargs.items()
                ]
            )
        )
        if len(index.kwargs)
        else "",
    }
    return text


@renderers.dispatch_for(ops.CreateUniqueConstraintOp)
def _add_unique_constraint(
    autogen_context: "AutogenContext", op: "ops.CreateUniqueConstraintOp"
) -> List[str]:
    return [_uq_constraint(op.to_constraint(), autogen_context, True)]


@renderers.dispatch_for(ops.CreateForeignKeyOp)
def _add_fk_constraint(
    autogen_context: "AutogenContext", op: "ops.CreateForeignKeyOp"
) -> str:

    args = [repr(_render_gen_name(autogen_context, op.constraint_name))]
    if not autogen_context._has_batch:
        args.append(repr(_ident(op.source_table)))

    args.extend(
        [
            repr(_ident(op.referent_table)),
            repr([_ident(col) for col in op.local_cols]),
            repr([_ident(col) for col in op.remote_cols]),
        ]
    )
    kwargs = [
        "referent_schema",
        "onupdate",
        "ondelete",
        "initially",
        "deferrable",
        "use_alter",
    ]
    if not autogen_context._has_batch:
        kwargs.insert(0, "source_schema")

    for k in kwargs:
        if k in op.kw:
            value = op.kw[k]
            if value is not None:
                args.append("%s=%r" % (k, value))

    return "%(prefix)screate_foreign_key(%(args)s)" % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "args": ", ".join(args),
    }


@renderers.dispatch_for(ops.CreatePrimaryKeyOp)
def _add_pk_constraint(constraint, autogen_context):
    raise NotImplementedError()


@renderers.dispatch_for(ops.CreateCheckConstraintOp)
def _add_check_constraint(constraint, autogen_context):
    raise NotImplementedError()


@renderers.dispatch_for(ops.DropConstraintOp)
def _drop_constraint(
    autogen_context: "AutogenContext", op: "ops.DropConstraintOp"
) -> str:

    if autogen_context._has_batch:
        template = "%(prefix)sdrop_constraint" "(%(name)r, type_=%(type)r)"
    else:
        template = (
            "%(prefix)sdrop_constraint"
            "(%(name)r, '%(table_name)s'%(schema)s, type_=%(type)r)"
        )

    text = template % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "name": _render_gen_name(autogen_context, op.constraint_name),
        "table_name": _ident(op.table_name),
        "type": op.constraint_type,
        "schema": (", schema=%r" % _ident(op.schema)) if op.schema else "",
    }
    return text


@renderers.dispatch_for(ops.AddColumnOp)
def _add_column(
    autogen_context: "AutogenContext", op: "ops.AddColumnOp"
) -> str:

    schema, tname, column = op.schema, op.table_name, op.column
    if autogen_context._has_batch:
        template = "%(prefix)sadd_column(%(column)s)"
    else:
        template = "%(prefix)sadd_column(%(tname)r, %(column)s"
        if schema:
            template += ", schema=%(schema)r"
        template += ")"
    text = template % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "tname": tname,
        "column": _render_column(column, autogen_context),
        "schema": schema,
    }
    return text


@renderers.dispatch_for(ops.DropColumnOp)
def _drop_column(
    autogen_context: "AutogenContext", op: "ops.DropColumnOp"
) -> str:

    schema, tname, column_name = op.schema, op.table_name, op.column_name

    if autogen_context._has_batch:
        template = "%(prefix)sdrop_column(%(cname)r)"
    else:
        template = "%(prefix)sdrop_column(%(tname)r, %(cname)r"
        if schema:
            template += ", schema=%(schema)r"
        template += ")"

    text = template % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "tname": _ident(tname),
        "cname": _ident(column_name),
        "schema": _ident(schema),
    }
    return text


@renderers.dispatch_for(ops.AlterColumnOp)
def _alter_column(
    autogen_context: "AutogenContext", op: "ops.AlterColumnOp"
) -> str:

    tname = op.table_name
    cname = op.column_name
    server_default = op.modify_server_default
    type_ = op.modify_type
    nullable = op.modify_nullable
    comment = op.modify_comment
    autoincrement = op.kw.get("autoincrement", None)
    existing_type = op.existing_type
    existing_nullable = op.existing_nullable
    existing_comment = op.existing_comment
    existing_server_default = op.existing_server_default
    schema = op.schema

    indent = " " * 11

    if autogen_context._has_batch:
        template = "%(prefix)salter_column(%(cname)r"
    else:
        template = "%(prefix)salter_column(%(tname)r, %(cname)r"

    text = template % {
        "prefix": _alembic_autogenerate_prefix(autogen_context),
        "tname": tname,
        "cname": cname,
    }
    if existing_type is not None:
        text += ",\n%sexisting_type=%s" % (
            indent,
            _repr_type(existing_type, autogen_context),
        )
    if server_default is not False:
        rendered = _render_server_default(server_default, autogen_context)
        text += ",\n%sserver_default=%s" % (indent, rendered)

    if type_ is not None:
        text += ",\n%stype_=%s" % (indent, _repr_type(type_, autogen_context))
    if nullable is not None:
        text += ",\n%snullable=%r" % (indent, nullable)
    if comment is not False:
        text += ",\n%scomment=%r" % (indent, comment)
    if existing_comment is not None:
        text += ",\n%sexisting_comment=%r" % (indent, existing_comment)
    if nullable is None and existing_nullable is not None:
        text += ",\n%sexisting_nullable=%r" % (indent, existing_nullable)
    if autoincrement is not None:
        text += ",\n%sautoincrement=%r" % (indent, autoincrement)
    if server_default is False and existing_server_default:
        rendered = _render_server_default(
            existing_server_default, autogen_context
        )
        text += ",\n%sexisting_server_default=%s" % (indent, rendered)
    if schema and not autogen_context._has_batch:
        text += ",\n%sschema=%r" % (indent, schema)
    text += ")"
    return text


class _f_name:
    def __init__(self, prefix: str, name: conv) -> None:
        self.prefix = prefix
        self.name = name

    def __repr__(self) -> str:
        return "%sf(%r)" % (self.prefix, _ident(self.name))


def _ident(name: Optional[Union["quoted_name", str]]) -> Optional[str]:
    """produce a __repr__() object for a string identifier that may
    use quoted_name() in SQLAlchemy 0.9 and greater.

    The issue worked around here is that quoted_name() doesn't have
    very good repr() behavior by itself when unicode is involved.

    """
    if name is None:
        return name
    elif isinstance(name, sql.elements.quoted_name):
        return compat.text_type(name)
    elif isinstance(name, compat.string_types):
        return name


def _render_potential_expr(
    value: Any,
    autogen_context: "AutogenContext",
    wrap_in_text: bool = True,
    is_server_default: bool = False,
) -> str:
    if isinstance(value, sql.ClauseElement):

        if wrap_in_text:
            template = "%(prefix)stext(%(sql)r)"
        else:
            template = "%(sql)r"

        return template % {
            "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
            "sql": autogen_context.migration_context.impl.render_ddl_sql_expr(
                value, is_server_default=is_server_default
            ),
        }

    else:
        return repr(value)


def _get_index_rendered_expressions(
    idx: "Index", autogen_context: "AutogenContext"
) -> List[str]:
    return [
        repr(_ident(getattr(exp, "name", None)))
        if isinstance(exp, sa_schema.Column)
        else _render_potential_expr(exp, autogen_context)
        for exp in idx.expressions
    ]


def _uq_constraint(
    constraint: "UniqueConstraint",
    autogen_context: "AutogenContext",
    alter: bool,
) -> str:
    opts: List[Tuple[str, Any]] = []

    has_batch = autogen_context._has_batch

    if constraint.deferrable:
        opts.append(("deferrable", str(constraint.deferrable)))
    if constraint.initially:
        opts.append(("initially", str(constraint.initially)))
    if not has_batch and alter and constraint.table.schema:
        opts.append(("schema", _ident(constraint.table.schema)))
    if not alter and constraint.name:
        opts.append(
            ("name", _render_gen_name(autogen_context, constraint.name))
        )

    if alter:
        args = [repr(_render_gen_name(autogen_context, constraint.name))]
        if not has_batch:
            args += [repr(_ident(constraint.table.name))]
        args.append(repr([_ident(col.name) for col in constraint.columns]))
        args.extend(["%s=%r" % (k, v) for k, v in opts])
        return "%(prefix)screate_unique_constraint(%(args)s)" % {
            "prefix": _alembic_autogenerate_prefix(autogen_context),
            "args": ", ".join(args),
        }
    else:
        args = [repr(_ident(col.name)) for col in constraint.columns]
        args.extend(["%s=%r" % (k, v) for k, v in opts])
        return "%(prefix)sUniqueConstraint(%(args)s)" % {
            "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
            "args": ", ".join(args),
        }


def _user_autogenerate_prefix(autogen_context, target):
    prefix = autogen_context.opts["user_module_prefix"]
    if prefix is None:
        return "%s." % target.__module__
    else:
        return prefix


def _sqlalchemy_autogenerate_prefix(autogen_context: "AutogenContext") -> str:
    return autogen_context.opts["sqlalchemy_module_prefix"] or ""


def _alembic_autogenerate_prefix(autogen_context: "AutogenContext") -> str:
    if autogen_context._has_batch:
        return "batch_op."
    else:
        return autogen_context.opts["alembic_module_prefix"] or ""


def _user_defined_render(
    type_: str, object_: Any, autogen_context: "AutogenContext"
) -> Union[str, "Literal[False]"]:
    if "render_item" in autogen_context.opts:
        render = autogen_context.opts["render_item"]
        if render:
            rendered = render(type_, object_, autogen_context)
            if rendered is not False:
                return rendered
    return False


def _render_column(column: "Column", autogen_context: "AutogenContext") -> str:
    rendered = _user_defined_render("column", column, autogen_context)
    if rendered is not False:
        return rendered

    args: List[str] = []
    opts: List[Tuple[str, Any]] = []

    if column.server_default:

        rendered = _render_server_default(  # type:ignore[assignment]
            column.server_default, autogen_context
        )
        if rendered:
            if _should_render_server_default_positionally(
                column.server_default
            ):
                args.append(rendered)
            else:
                opts.append(("server_default", rendered))

    if (
        column.autoincrement is not None
        and column.autoincrement != sqla_compat.AUTOINCREMENT_DEFAULT
    ):
        opts.append(("autoincrement", column.autoincrement))

    if column.nullable is not None:
        opts.append(("nullable", column.nullable))

    if column.system:
        opts.append(("system", column.system))

    comment = column.comment
    if comment:
        opts.append(("comment", "%r" % comment))

    # TODO: for non-ascii colname, assign a "key"
    return "%(prefix)sColumn(%(name)r, %(type)s, %(args)s%(kwargs)s)" % {
        "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
        "name": _ident(column.name),
        "type": _repr_type(column.type, autogen_context),
        "args": ", ".join([str(arg) for arg in args]) + ", " if args else "",
        "kwargs": (
            ", ".join(
                ["%s=%s" % (kwname, val) for kwname, val in opts]
                + [
                    "%s=%s"
                    % (key, _render_potential_expr(val, autogen_context))
                    for key, val in sqla_compat._column_kwargs(column).items()
                ]
            )
        ),
    }


def _should_render_server_default_positionally(
    server_default: Union["Computed", "DefaultClause"]
) -> bool:
    return sqla_compat._server_default_is_computed(
        server_default
    ) or sqla_compat._server_default_is_identity(server_default)


def _render_server_default(
    default: Optional[
        Union["FetchedValue", str, "TextClause", "ColumnElement"]
    ],
    autogen_context: "AutogenContext",
    repr_: bool = True,
) -> Optional[str]:
    rendered = _user_defined_render("server_default", default, autogen_context)
    if rendered is not False:
        return rendered

    if sqla_compat._server_default_is_computed(default):
        return _render_computed(cast("Computed", default), autogen_context)
    elif sqla_compat._server_default_is_identity(default):
        return _render_identity(cast("Identity", default), autogen_context)
    elif isinstance(default, sa_schema.DefaultClause):
        if isinstance(default.arg, compat.string_types):
            default = default.arg
        else:
            return _render_potential_expr(
                default.arg, autogen_context, is_server_default=True
            )

    if isinstance(default, string_types) and repr_:
        default = repr(re.sub(r"^'|'$", "", default))

    return cast(str, default)


def _render_computed(
    computed: "Computed", autogen_context: "AutogenContext"
) -> str:
    text = _render_potential_expr(
        computed.sqltext, autogen_context, wrap_in_text=False
    )

    kwargs = {}
    if computed.persisted is not None:
        kwargs["persisted"] = computed.persisted
    return "%(prefix)sComputed(%(text)s, %(kwargs)s)" % {
        "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
        "text": text,
        "kwargs": (", ".join("%s=%s" % pair for pair in kwargs.items())),
    }


def _render_identity(
    identity: "Identity", autogen_context: "AutogenContext"
) -> str:
    # always=None means something different than always=False
    kwargs = OrderedDict(always=identity.always)
    if identity.on_null is not None:
        kwargs["on_null"] = identity.on_null
    kwargs.update(_get_identity_options(identity))

    return "%(prefix)sIdentity(%(kwargs)s)" % {
        "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
        "kwargs": (", ".join("%s=%s" % pair for pair in kwargs.items())),
    }


def _get_identity_options(identity_options: "Identity") -> OrderedDict:
    kwargs = OrderedDict()
    for attr in sqla_compat._identity_options_attrs:
        value = getattr(identity_options, attr, None)
        if value is not None:
            kwargs[attr] = value
    return kwargs


def _repr_type(type_: "TypeEngine", autogen_context: "AutogenContext") -> str:
    rendered = _user_defined_render("type", type_, autogen_context)
    if rendered is not False:
        return rendered

    if hasattr(autogen_context.migration_context, "impl"):
        impl_rt = autogen_context.migration_context.impl.render_type(
            type_, autogen_context
        )
    else:
        impl_rt = None

    mod = type(type_).__module__
    imports = autogen_context.imports
    if mod.startswith("sqlalchemy.dialects"):
        match = re.match(r"sqlalchemy\.dialects\.(\w+)", mod)
        assert match is not None
        dname = match.group(1)
        if imports is not None:
            imports.add("from sqlalchemy.dialects import %s" % dname)
        if impl_rt:
            return impl_rt
        else:
            return "%s.%r" % (dname, type_)
    elif impl_rt:
        return impl_rt
    elif mod.startswith("sqlalchemy."):
        if type(type_) is sqltypes.Variant:
            return _render_Variant_type(type_, autogen_context)
        if "_render_%s_type" % type_.__visit_name__ in globals():
            fn = globals()["_render_%s_type" % type_.__visit_name__]
            return fn(type_, autogen_context)
        else:
            prefix = _sqlalchemy_autogenerate_prefix(autogen_context)
            return "%s%r" % (prefix, type_)
    else:
        prefix = _user_autogenerate_prefix(autogen_context, type_)
        return "%s%r" % (prefix, type_)


def _render_ARRAY_type(
    type_: "ARRAY", autogen_context: "AutogenContext"
) -> str:
    return cast(
        str,
        _render_type_w_subtype(
            type_, autogen_context, "item_type", r"(.+?\()"
        ),
    )


def _render_Variant_type(
    type_: "Variant", autogen_context: "AutogenContext"
) -> str:
    base = _repr_type(type_.impl, autogen_context)
    assert base is not None and base is not False
    for dialect in sorted(type_.mapping):
        typ = type_.mapping[dialect]
        base += ".with_variant(%s, %r)" % (
            _repr_type(typ, autogen_context),
            dialect,
        )
    return base


def _render_type_w_subtype(
    type_: "TypeEngine",
    autogen_context: "AutogenContext",
    attrname: str,
    regexp: str,
    prefix: Optional[str] = None,
) -> Union[Optional[str], "Literal[False]"]:
    outer_repr = repr(type_)
    inner_type = getattr(type_, attrname, None)
    if inner_type is None:
        return False

    inner_repr = repr(inner_type)

    inner_repr = re.sub(r"([\(\)])", r"\\\1", inner_repr)
    sub_type = _repr_type(getattr(type_, attrname), autogen_context)
    outer_type = re.sub(regexp + inner_repr, r"\1%s" % sub_type, outer_repr)

    if prefix:
        return "%s%s" % (prefix, outer_type)

    mod = type(type_).__module__
    if mod.startswith("sqlalchemy.dialects"):
        match = re.match(r"sqlalchemy\.dialects\.(\w+)", mod)
        assert match is not None
        dname = match.group(1)
        return "%s.%s" % (dname, outer_type)
    elif mod.startswith("sqlalchemy"):
        prefix = _sqlalchemy_autogenerate_prefix(autogen_context)
        return "%s%s" % (prefix, outer_type)
    else:
        return None


_constraint_renderers = util.Dispatcher()


def _render_constraint(
    constraint: "Constraint",
    autogen_context: "AutogenContext",
    namespace_metadata: Optional["MetaData"],
) -> Optional[str]:
    try:
        renderer = _constraint_renderers.dispatch(constraint)
    except ValueError:
        util.warn("No renderer is established for object %r" % constraint)
        return "[Unknown Python object %r]" % constraint
    else:
        return renderer(constraint, autogen_context, namespace_metadata)


@_constraint_renderers.dispatch_for(sa_schema.PrimaryKeyConstraint)
def _render_primary_key(
    constraint: "PrimaryKeyConstraint",
    autogen_context: "AutogenContext",
    namespace_metadata: Optional["MetaData"],
) -> Optional[str]:
    rendered = _user_defined_render("primary_key", constraint, autogen_context)
    if rendered is not False:
        return rendered

    if not constraint.columns:
        return None

    opts = []
    if constraint.name:
        opts.append(
            ("name", repr(_render_gen_name(autogen_context, constraint.name)))
        )
    return "%(prefix)sPrimaryKeyConstraint(%(args)s)" % {
        "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
        "args": ", ".join(
            [repr(c.name) for c in constraint.columns]
            + ["%s=%s" % (kwname, val) for kwname, val in opts]
        ),
    }


def _fk_colspec(
    fk: "ForeignKey",
    metadata_schema: Optional[str],
    namespace_metadata: "MetaData",
) -> str:
    """Implement a 'safe' version of ForeignKey._get_colspec() that
    won't fail if the remote table can't be resolved.

    """
    colspec = fk._get_colspec()  # type:ignore[attr-defined]
    tokens = colspec.split(".")
    tname, colname = tokens[-2:]

    if metadata_schema is not None and len(tokens) == 2:
        table_fullname = "%s.%s" % (metadata_schema, tname)
    else:
        table_fullname = ".".join(tokens[0:-1])

    if (
        not fk.link_to_name
        and fk.parent is not None
        and fk.parent.table is not None
    ):
        # try to resolve the remote table in order to adjust for column.key.
        # the FK constraint needs to be rendered in terms of the column
        # name.

        if table_fullname in namespace_metadata.tables:
            col = namespace_metadata.tables[table_fullname].c.get(colname)
            if col is not None:
                colname = _ident(col.name)

    colspec = "%s.%s" % (table_fullname, colname)

    return colspec


def _populate_render_fk_opts(
    constraint: "ForeignKeyConstraint", opts: List[Tuple[str, str]]
) -> None:

    if constraint.onupdate:
        opts.append(("onupdate", repr(constraint.onupdate)))
    if constraint.ondelete:
        opts.append(("ondelete", repr(constraint.ondelete)))
    if constraint.initially:
        opts.append(("initially", repr(constraint.initially)))
    if constraint.deferrable:
        opts.append(("deferrable", repr(constraint.deferrable)))
    if constraint.use_alter:
        opts.append(("use_alter", repr(constraint.use_alter)))


@_constraint_renderers.dispatch_for(sa_schema.ForeignKeyConstraint)
def _render_foreign_key(
    constraint: "ForeignKeyConstraint",
    autogen_context: "AutogenContext",
    namespace_metadata: "MetaData",
) -> Optional[str]:
    rendered = _user_defined_render("foreign_key", constraint, autogen_context)
    if rendered is not False:
        return rendered

    opts = []
    if constraint.name:
        opts.append(
            ("name", repr(_render_gen_name(autogen_context, constraint.name)))
        )

    _populate_render_fk_opts(constraint, opts)

    apply_metadata_schema = namespace_metadata.schema
    return (
        "%(prefix)sForeignKeyConstraint([%(cols)s], "
        "[%(refcols)s], %(args)s)"
        % {
            "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
            "cols": ", ".join(
                "%r" % _ident(cast("Column", f.parent).name)
                for f in constraint.elements
            ),
            "refcols": ", ".join(
                repr(_fk_colspec(f, apply_metadata_schema, namespace_metadata))
                for f in constraint.elements
            ),
            "args": ", ".join(
                ["%s=%s" % (kwname, val) for kwname, val in opts]
            ),
        }
    )


@_constraint_renderers.dispatch_for(sa_schema.UniqueConstraint)
def _render_unique_constraint(
    constraint: "UniqueConstraint",
    autogen_context: "AutogenContext",
    namespace_metadata: Optional["MetaData"],
) -> str:
    rendered = _user_defined_render("unique", constraint, autogen_context)
    if rendered is not False:
        return rendered

    return _uq_constraint(constraint, autogen_context, False)


@_constraint_renderers.dispatch_for(sa_schema.CheckConstraint)
def _render_check_constraint(
    constraint: "CheckConstraint",
    autogen_context: "AutogenContext",
    namespace_metadata: Optional["MetaData"],
) -> Optional[str]:
    rendered = _user_defined_render("check", constraint, autogen_context)
    if rendered is not False:
        return rendered

    # detect the constraint being part of
    # a parent type which is probably in the Table already.
    # ideally SQLAlchemy would give us more of a first class
    # way to detect this.
    if (
        constraint._create_rule  # type:ignore[attr-defined]
        and hasattr(
            constraint._create_rule, "target"  # type:ignore[attr-defined]
        )
        and isinstance(
            constraint._create_rule.target,  # type:ignore[attr-defined]
            sqltypes.TypeEngine,
        )
    ):
        return None
    opts = []
    if constraint.name:
        opts.append(
            ("name", repr(_render_gen_name(autogen_context, constraint.name)))
        )
    return "%(prefix)sCheckConstraint(%(sqltext)s%(opts)s)" % {
        "prefix": _sqlalchemy_autogenerate_prefix(autogen_context),
        "opts": ", " + (", ".join("%s=%s" % (k, v) for k, v in opts))
        if opts
        else "",
        "sqltext": _render_potential_expr(
            constraint.sqltext, autogen_context, wrap_in_text=False
        ),
    }


@renderers.dispatch_for(ops.ExecuteSQLOp)
def _execute_sql(
    autogen_context: "AutogenContext", op: "ops.ExecuteSQLOp"
) -> str:
    if not isinstance(op.sqltext, string_types):
        raise NotImplementedError(
            "Autogenerate rendering of SQL Expression language constructs "
            "not supported here; please use a plain SQL string"
        )
    return "op.execute(%r)" % op.sqltext


renderers = default_renderers.branch()