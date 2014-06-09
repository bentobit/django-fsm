# -*- coding: utf-8 -*-
"""
State tracking functionality for django models
"""
import inspect
from functools import wraps

from django.db import models
from django.db.models.loading import get_model
from django.db.models.signals import class_prepared
from django.utils.functional import curry
from django_fsm.signals import pre_transition, post_transition


__all__ = ["TransitionNotAllowed", "FSMFieldMixin", "FSMField",
           'FSMIntegerField', 'FSMKeyField', 'transition',
           'can_proceed', 'has_transition_perm']


# South support; see http://south.aeracode.org/docs/tutorial/part4.html#simple-inheritance
try:
    from south.modelsinspector import add_introspection_rules
except ImportError:
    pass
else:
    add_introspection_rules([], [r"^django_fsm\.FSMField"])
    add_introspection_rules([], [r"^django_fsm\.FSMIntegerField"])
    add_introspection_rules([], [r"^django_fsm\.FSMKeyField"])


class TransitionNotAllowed(Exception):
    """Raise when a transition is not allowed"""


class Transition(object):
    def __init__(self, method, source, target, conditions, permission, custom):
        self.method = method
        self.source = source
        self.target = target
        self.conditions = conditions
        self.permission = permission
        self.custom = custom

    @property
    def name(self):
        return self.method.__name__

    def has_perm(self, user):
        if not self.permission:
            return True
        elif callable(self.permission) and self.permission(user):
            return True
        elif user.has_perm(self.permission):
            return True
        else:
            return False


def get_available_FIELD_transitions(instance, field):
    """
    List of transitions available in current model state
    with all conditions met
    """
    curr_state = field.get_state(instance)
    transitions = field.transitions[instance.__class__]

    for name, transition in transitions.items():
        meta = transition._django_fsm

        for state in [curr_state, '*']:
            if state in meta.transitions:
                transition = meta.transitions[state]
                if all(map(lambda condition: condition(instance), transition.conditions)):
                    yield transition


def get_all_FIELD_transitions(instance, field):
    """
    List of all transitions available in current model state
    """
    return field.get_all_transitions(instance.__class__)


def get_available_user_FIELD_transitions(instance, user, field):
    """
    List of transitions available in current model state
    with all conditions met and user have rights on it
    """
    for transition in get_available_FIELD_transitions(instance, field):
        if transition.has_perm(user):
            yield transition


class FSMMeta(object):
    """
    Models methods transitions meta information
    """
    def __init__(self, field, method):
        self.field = field
        self.transitions = {}  # source -> Transition

    def get_transition(self, source):
        transition = self.transitions.get(source, None)
        if transition is None:
            transition = self.transitions.get('*', None)
        return transition

    def add_transition(self, method, source, target, conditions=[], permission=None, custom={}):
        if source in self.transitions:
            raise AssertionError('Duplicate transition for {0} state'.format(source))

        self.transitions[source] = Transition(
            method=method,
            source=source,
            target=target,
            conditions=conditions,
            permission=permission,
            custom=custom)

    def has_transition(self, state):
        """
        Lookup if any transition exists from current model state using current method
        """
        return state in self.transitions or '*' in self.transitions

    def conditions_met(self, instance, state):
        """
        Check if all conditions have been met
        """
        transition = self.get_transition(state)

        if transition is None:
            return False
        elif transition.conditions is None:
            return True
        else:
            return all(map(lambda condition: condition(instance), transition.conditions))

    def has_transition_perm(self, instance, state, user):
        transition = self.get_transition(state)

        if not transition:
            return False
        else:
            return transition.has_perm(user)

    def next_state(self, current_state):
        transition = self.get_transition(current_state)

        if transition is None:
            raise TransitionNotAllowed('No transition from {0}'.format(current_state))

        return transition.target


class FSMFieldDescriptor(object):
    def __init__(self, field):
        self.field = field

    def __get__(self, instance, type=None):
        if instance is None:
            #raise AttributeError('Can only be accessed via an instance.')
            return
        return self.field.get_state(instance)

    def __set__(self, instance, value):
        if self.field.protected and self.field.name in instance.__dict__:
            raise AttributeError('Direct {0} modification is not allowed'.format(self.field.name))

        # Update state
        self.field.set_proxy(instance, value)
        self.field.set_state(instance, value)


class FSMFieldMixin(object):
    descriptor_class = FSMFieldDescriptor

    def __init__(self, *args, **kwargs):
        self.protected = kwargs.pop('protected', False)
        self.transitions = {}  # cls -> (transitions name -> method)
        self.state_proxy = {}  # state -> ProxyClsRef

        state_choices = kwargs.pop('state_choices', None)
        choices = kwargs.get('choices', None)
        if state_choices is not None and choices is not None:
            raise ValueError('Use one of choices or state_choces value')

        if state_choices is not None:
            choices = []
            for state, title, proxy_cls_ref in state_choices:
                choices.append((state, title))
                self.state_proxy[state] = proxy_cls_ref
            kwargs['choices'] = choices

        super(FSMFieldMixin, self).__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super(FSMFieldMixin, self).deconstruct()
        if self.protected:
            kwargs['protected'] = self.protected
        return name, path, args, kwargs

    def get_state(self, instance):
        return instance.__dict__[self.name]

    def set_state(self, instance, state):
        instance.__dict__[self.name] = state

    def set_proxy(self, instance, state):
        """
        Change class
        """
        if state in self.state_proxy:
            state_proxy = self.state_proxy[state]

            try:
                app_label, model_name = state_proxy.split(".")
            except ValueError:
                # If we can't split, assume a model in current app
                app_label = instance._meta.app_label
                model_name = state_proxy

            model = get_model(app_label, model_name)
            if model is None:
                raise ValueError('No model found {0}'.format(state_proxy))

            instance.__class__ = model
        
    def change_state(self, instance, method, *args, **kwargs):
        meta = method._django_fsm
        method_name = method.__name__
        current_state = self.get_state(instance)

        if not (meta.has_transition(current_state) and meta.conditions_met(instance, current_state)):
            raise TransitionNotAllowed(
                "Can't switch from state '{0}' using method '{1}'".format(current_state, method_name))

        next_state = meta.next_state(current_state)

        signal_kwargs = {
            'sender': instance.__class__,
            'instance': instance,
            'name': method_name,
            'source': current_state,
            'target': next_state
        }

        pre_transition.send(**signal_kwargs)

        result = method(instance, *args, **kwargs)
        if next_state:
            self.set_proxy(instance, next_state)
            self.set_state(instance, next_state)

        post_transition.send(**signal_kwargs)

        return result

    def get_all_transitions(self, instance_cls):
        """
        Returns [(source, target, name, method)] for all field transitions
        """
        transitions = self.transitions[instance_cls]

        for name, transition in transitions.items():
            meta = transition._django_fsm

            for transition in meta.transitions.values():
                yield transition

    def contribute_to_class(self, cls, name, virtual_only=False):
        self.base_cls = cls

        super(FSMFieldMixin, self).contribute_to_class(cls, name, virtual_only=virtual_only)
        setattr(cls, self.name, self.descriptor_class(self))
        setattr(cls, 'get_all_{0}_transitions'.format(self.name),
                curry(get_all_FIELD_transitions, field=self))
        setattr(cls, 'get_available_{0}_transitions'.format(self.name),
                curry(get_available_FIELD_transitions, field=self))
        setattr(cls, 'get_available_user_{0}_transitions'.format(self.name),
                curry(get_available_user_FIELD_transitions, field=self))

        class_prepared.connect(self._collect_transitions)

    def _collect_transitions(self, *args, **kwargs):
        sender = kwargs['sender']

        if not issubclass(sender, self.base_cls):
            return

        def is_field_transition_method(attr):
            return (inspect.ismethod(attr) or inspect.isfunction(attr)) \
                and hasattr(attr, '_django_fsm') \
                and attr._django_fsm.field in [self, self.name]

        sender_transitions = {}
        transitions = inspect.getmembers(sender, predicate=is_field_transition_method)
        for method_name, method in transitions:
            method._django_fsm.field = self
            sender_transitions[method_name] = method

        self.transitions[sender] = sender_transitions


class FSMField(FSMFieldMixin, models.CharField):
    """
    State Machine support for Django model as CharField
    """
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_length', 50)
        super(FSMField, self).__init__(*args, **kwargs)


class FSMIntegerField(FSMFieldMixin, models.IntegerField):
    """
    Same as FSMField, but stores the state value in an IntegerField.
    db_index is True by default.
    """
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('db_index', True)
        super(FSMIntegerField, self).__init__(*args, **kwargs)


class FSMKeyField(FSMFieldMixin, models.ForeignKey):
    """
    State Machine support for Django model
    """
    def get_state(self, instance):
        return instance.__dict__[self.attname]

    def set_state(self, instance, state):
        instance.__dict__[self.attname] = self.to_python(state)


def transition(field, source='*', target=None, conditions=[], permission=None, custom={}):
    """
    Method decorator for mark allowed transitions

    Set target to None if current state needs to be validated and
    has not changed after the function call
    """
    def inner_transition(func):
        fsm_meta = getattr(func, '_django_fsm', None)
        if not fsm_meta:
            fsm_meta = FSMMeta(field=field, method=func)
            setattr(func, '_django_fsm', fsm_meta)

        @wraps(func)
        def _change_state(instance, *args, **kwargs):
            return fsm_meta.field.change_state(instance, func, *args, **kwargs)

        if isinstance(source, (list, tuple)):
            for state in source:
                func._django_fsm.add_transition(func, state, target, conditions, permission, custom)
        else:
            func._django_fsm.add_transition(func, source, target, conditions, permission, custom)

        return _change_state

    return inner_transition


def can_proceed(bound_method):
    """
    Returns True if model in state allows to call bound_method
    """
    if not hasattr(bound_method, '_django_fsm'):
        raise TypeError('%s method is not transition' % bound_method.im_func.__name__)

    meta = bound_method._django_fsm
    im_self = getattr(bound_method, 'im_self', getattr(bound_method, '__self__'))
    current_state = meta.field.get_state(im_self)

    return meta.has_transition(current_state) and meta.conditions_met(im_self, current_state)


def has_transition_perm(bound_method, user):
    """
    Returns True if model in state allows to call bound_method and user have rights on it
    """
    if not hasattr(bound_method, '_django_fsm'):
        raise TypeError('%s method is not transition' % bound_method.im_func.__name__)

    meta = bound_method._django_fsm
    im_self = getattr(bound_method, 'im_self', getattr(bound_method, '__self__'))
    current_state = meta.field.get_state(im_self)

    return (meta.has_transition(current_state)
            and meta.conditions_met(im_self, current_state)
            and meta.has_transition_perm(im_self, current_state, user))
