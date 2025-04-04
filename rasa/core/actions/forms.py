from enum import Enum
from typing import Text, List, Optional, Union, Any, Dict, Tuple
import logging

from rasa.core.actions import action
from rasa.core.actions.loops import LoopAction
from rasa.core.channels import OutputChannel
from rasa.core.constants import REQUESTED_SLOT, UTTER_PREFIX
from rasa.core.domain import Domain

from rasa.core.actions.action import (
    ActionExecutionRejection,
    RemoteAction,
    ACTION_LISTEN_NAME,
)
from rasa.core.events import Event, SlotSet, ActionExecuted
from rasa.core.nlg import NaturalLanguageGenerator
from rasa.core.trackers import DialogueStateTracker
from rasa.utils.endpoints import EndpointConfig

logger = logging.getLogger(__name__)


class SlotMapping(Enum):
    FROM_ENTITY = 0
    FROM_INTENT = 1
    FROM_TRIGGER_INTENT = 2
    FROM_TEXT = 3

    def __str__(self) -> Text:
        return self.name.lower()


class FormAction(LoopAction):
    def __init__(
        self, form_name: Text, action_endpoint: Optional[EndpointConfig]
    ) -> None:
        self._form_name = form_name
        self.action_endpoint = action_endpoint
        self._domain: Optional[Domain] = None

    def name(self) -> Text:
        return self._form_name

    def required_slots(self, domain: Domain) -> List[Text]:
        """A list of required slots that the form has to fill.

        Returns:
            A list of slot names.
        """
        return list(domain.slot_mapping_for_form(self.name()).keys())

    def from_entity(
        self,
        entity: Text,
        intent: Optional[Union[Text, List[Text]]] = None,
        not_intent: Optional[Union[Text, List[Text]]] = None,
        role: Optional[Text] = None,
        group: Optional[Text] = None,
    ) -> Dict[Text, Any]:
        """A dictionary for slot mapping to extract slot value.

        From:
        - an extracted entity
        - conditioned on
            - intent if it is not None
            - not_intent if it is not None,
                meaning user intent should not be this intent
            - role if it is not None
            - group if it is not None
        """

        intent, not_intent = self._list_intents(intent, not_intent)

        return {
            "type": str(SlotMapping.FROM_ENTITY),
            "entity": entity,
            "intent": intent,
            "not_intent": not_intent,
            "role": role,
            "group": group,
        }

    def get_mappings_for_slot(
        self, slot_to_fill: Text, domain: Domain
    ) -> List[Dict[Text, Any]]:
        """Get mappings for requested slot.

        If None, map requested slot to an entity with the same name
        """

        requested_slot_mappings = self._to_list(
            domain.slot_mapping_for_form(self.name()).get(
                slot_to_fill, self.from_entity(slot_to_fill)
            )
        )
        # check provided slot mappings
        for requested_slot_mapping in requested_slot_mappings:
            if (
                not isinstance(requested_slot_mapping, dict)
                or requested_slot_mapping.get("type") is None
            ):
                raise TypeError("Provided incompatible slot mapping")

        return requested_slot_mappings

    @staticmethod
    def intent_is_desired(
        requested_slot_mapping: Dict[Text, Any], tracker: "DialogueStateTracker"
    ) -> bool:
        """Check whether user intent matches intent conditions"""

        mapping_intents = requested_slot_mapping.get("intent", [])
        mapping_not_intents = requested_slot_mapping.get("not_intent", [])

        intent = tracker.latest_message.intent.get("name")

        intent_not_blocked = not mapping_intents and intent not in mapping_not_intents

        return intent_not_blocked or intent in mapping_intents

    def entity_is_desired(
        self,
        requested_slot_mapping: Dict[Text, Any],
        slot: Text,
        entity_type_of_slot_to_fill: Optional[Text],
        tracker: "DialogueStateTracker",
    ) -> bool:
        """Check whether slot should be filled by an entity in the input or not.

        Args:
            requested_slot_mapping: Slot mapping.
            slot: The slot to be filled.
            entity_type_of_slot_to_fill: Entity type of slot to fill.
            tracker: The tracker.

        Returns:
            True, if slot should be filled, false otherwise.
        """

        # slot name is equal to the entity type
        slot_equals_entity = slot == requested_slot_mapping.get("entity")

        # use the custom slot mapping 'from_entity' defined by the user to check
        # whether we can fill a slot with an entity (only if a role or a group label
        # is set)
        if (
            requested_slot_mapping.get("role") is None
            and requested_slot_mapping.get("group") is None
        ) or entity_type_of_slot_to_fill != requested_slot_mapping.get("entity"):
            slot_fulfils_entity_mapping = False
        else:
            matching_values = self.get_entity_value(
                requested_slot_mapping.get("entity"),
                tracker,
                requested_slot_mapping.get("role"),
                requested_slot_mapping.get("group"),
            )
            slot_fulfils_entity_mapping = matching_values is not None

        return slot_equals_entity or slot_fulfils_entity_mapping

    @staticmethod
    def get_entity_value(
        name: Text,
        tracker: "DialogueStateTracker",
        role: Optional[Text] = None,
        group: Optional[Text] = None,
    ) -> Any:
        """Extract entities for given name and optional role and group.

        Args:
            name: entity type (name) of interest
            tracker: the tracker
            role: optional entity role of interest
            group: optional entity group of interest

        Returns:
            Value of entity.
        """
        # list is used to cover the case of list slot type
        value = list(
            tracker.get_latest_entity_values(name, entity_group=group, entity_role=role)
        )
        if len(value) == 0:
            value = None
        elif len(value) == 1:
            value = value[0]
        return value

    def extract_other_slots(
        self, tracker: DialogueStateTracker, domain: Domain
    ) -> Dict[Text, Any]:
        """Extract the values of the other slots
        if they are set by corresponding entities from the user input
        else return `None`.
        """
        slot_to_fill = tracker.get_slot(REQUESTED_SLOT)

        entity_type_of_slot_to_fill = self._get_entity_type_of_slot_to_fill(
            slot_to_fill, domain
        )

        slot_values = {}
        for slot in self.required_slots(domain):
            # look for other slots
            if slot != slot_to_fill:
                # list is used to cover the case of list slot type
                other_slot_mappings = self.get_mappings_for_slot(slot, domain)

                for other_slot_mapping in other_slot_mappings:
                    # check whether the slot should be filled by an entity in the input
                    should_fill_entity_slot = (
                        other_slot_mapping["type"] == str(SlotMapping.FROM_ENTITY)
                        and self.intent_is_desired(other_slot_mapping, tracker)
                        and self.entity_is_desired(
                            other_slot_mapping,
                            slot,
                            entity_type_of_slot_to_fill,
                            tracker,
                        )
                    )
                    # check whether the slot should be
                    # filled from trigger intent mapping
                    should_fill_trigger_slot = (
                        tracker.active_loop.get("name") != self.name()
                        and other_slot_mapping["type"]
                        == str(SlotMapping.FROM_TRIGGER_INTENT)
                        and self.intent_is_desired(other_slot_mapping, tracker)
                    )
                    if should_fill_entity_slot:
                        value = self.get_entity_value(
                            other_slot_mapping["entity"],
                            tracker,
                            other_slot_mapping.get("role"),
                            other_slot_mapping.get("group"),
                        )
                    elif should_fill_trigger_slot:
                        value = other_slot_mapping.get("value")
                    else:
                        value = None

                    if value is not None:
                        logger.debug(f"Extracted '{value}' for extra slot '{slot}'.")
                        slot_values[slot] = value
                        # this slot is done, check  next
                        break

        return slot_values

    def extract_requested_slot(
        self, tracker: "DialogueStateTracker", domain: Domain
    ) -> Dict[Text, Any]:
        """Extract the value of requested slot from a user input
        else return `None`.
        """
        slot_to_fill = tracker.get_slot(REQUESTED_SLOT)
        logger.debug(f"Trying to extract requested slot '{slot_to_fill}' ...")

        # get mapping for requested slot
        requested_slot_mappings = self.get_mappings_for_slot(slot_to_fill, domain)

        for requested_slot_mapping in requested_slot_mappings:
            logger.debug(f"Got mapping '{requested_slot_mapping}'")

            if self.intent_is_desired(requested_slot_mapping, tracker):
                mapping_type = requested_slot_mapping["type"]

                if mapping_type == str(SlotMapping.FROM_ENTITY):
                    value = self.get_entity_value(
                        requested_slot_mapping.get("entity"),
                        tracker,
                        requested_slot_mapping.get("role"),
                        requested_slot_mapping.get("group"),
                    )
                elif mapping_type == str(SlotMapping.FROM_INTENT):
                    value = requested_slot_mapping.get("value")
                elif mapping_type == str(SlotMapping.FROM_TRIGGER_INTENT):
                    # from_trigger_intent is only used on form activation
                    continue
                elif mapping_type == str(SlotMapping.FROM_TEXT):
                    value = tracker.latest_message.text
                else:
                    raise ValueError("Provided slot mapping type is not supported")

                if value is not None:
                    logger.debug(
                        f"Successfully extracted '{value}' for requested slot "
                        f"'{slot_to_fill}'"
                    )
                    return {slot_to_fill: value}

        logger.debug(f"Failed to extract requested slot '{slot_to_fill}'")
        return {}

    async def validate_slots(
        self,
        slot_dict: Dict[Text, Any],
        tracker: "DialogueStateTracker",
        domain: Domain,
        output_channel: OutputChannel,
        nlg: NaturalLanguageGenerator,
    ) -> List[Event]:
        """Validate the extracted slots.

        If a custom action is available for validating the slots, we call it to validate
        them. Otherwise there is no validation.

        Args:
            slot_dict: Extracted slots which are candidates to fill the slots required
                by the form.
            tracker: The current conversation tracker.
            domain: The current model domain.
            output_channel: The output channel which can be used to send messages
                to the user.
            nlg:  `NaturalLanguageGenerator` to use for response generation.

        Returns:
            The validation events including potential bot messages and `SlotSet` events
            for the validated slots.
        """

        events = [SlotSet(slot_name, value) for slot_name, value in slot_dict.items()]

        validate_name = f"validate_{self.name()}"

        if validate_name not in domain.action_names:
            return events

        _tracker = self._temporary_tracker(tracker, events, domain)
        _action = RemoteAction(validate_name, self.action_endpoint)
        validate_events = await _action.run(output_channel, nlg, _tracker, domain)

        validated_slot_names = [
            event.key for event in validate_events if isinstance(event, SlotSet)
        ]

        # If the custom action doesn't return a SlotSet event for an extracted slot
        # candidate we assume that it was valid. The custom action has to return a
        # SlotSet(slot_name, None) event to mark a Slot as invalid.
        return validate_events + [
            event for event in events if event.key not in validated_slot_names
        ]

    def _temporary_tracker(
        self,
        current_tracker: DialogueStateTracker,
        additional_events: List[Event],
        domain: Domain,
    ) -> DialogueStateTracker:
        return DialogueStateTracker.from_events(
            current_tracker.sender_id,
            current_tracker.events_after_latest_restart()
            # Insert form execution event so that it's clearly distinguishable which
            # events were newly added.
            + [ActionExecuted(self.name())] + additional_events,
            slots=domain.slots,
        )

    async def validate(
        self,
        tracker: "DialogueStateTracker",
        domain: Domain,
        output_channel: OutputChannel,
        nlg: NaturalLanguageGenerator,
    ) -> List[Event]:
        """Extract and validate value of requested slot.

        If nothing was extracted reject execution of the form action.
        Subclass this method to add custom validation and rejection logic
        """

        # extract other slots that were not requested
        # but set by corresponding entity or trigger intent mapping
        slot_values = self.extract_other_slots(tracker, domain)

        # extract requested slot
        slot_to_fill = tracker.get_slot(REQUESTED_SLOT)
        if slot_to_fill:
            slot_values.update(self.extract_requested_slot(tracker, domain))

            if not slot_values:
                # reject to execute the form action
                # if some slot was requested but nothing was extracted
                # it will allow other policies to predict another action
                raise ActionExecutionRejection(
                    self.name(),
                    f"Failed to extract slot {slot_to_fill} with action {self.name()}",
                )
        logger.debug(f"Validating extracted slots: {slot_values}")
        return await self.validate_slots(
            slot_values, tracker, domain, output_channel, nlg
        )

    async def request_next_slot(
        self,
        tracker: "DialogueStateTracker",
        domain: Domain,
        output_channel: OutputChannel,
        nlg: NaturalLanguageGenerator,
        events_so_far: List[Event],
    ) -> List[Event]:
        """Request the next slot and utter template if needed, else return `None`."""
        request_slot_events = []

        # If this is not `None` it means that the custom action specified a next slot
        # to request
        slot_to_request = next(
            (
                event.value
                for event in events_so_far
                if isinstance(event, SlotSet) and event.key == REQUESTED_SLOT
            ),
            None,
        )
        temp_tracker = self._temporary_tracker(tracker, events_so_far, domain)

        if not slot_to_request:
            slot_to_request = self._find_next_slot_to_request(temp_tracker, domain)
            request_slot_events.append(SlotSet(REQUESTED_SLOT, slot_to_request))

        if slot_to_request:
            bot_message_events = await self._ask_for_slot(
                domain, nlg, output_channel, slot_to_request, temp_tracker
            )
            return request_slot_events + bot_message_events

        # no more required slots to fill
        return [SlotSet(REQUESTED_SLOT, None)]

    def _find_next_slot_to_request(
        self, tracker: DialogueStateTracker, domain: Domain
    ) -> Optional[Text]:
        return next(
            (
                slot
                for slot in self.required_slots(domain)
                if self._should_request_slot(tracker, slot)
            ),
            None,
        )

    def _name_of_utterance(self, domain: Domain, slot_name: Text) -> Text:
        search_path = [
            f"action_ask_{self._form_name}_{slot_name}",
            f"{UTTER_PREFIX}ask_{self._form_name}_{slot_name}",
            f"action_ask_{slot_name}",
        ]

        found_actions = (
            action_name
            for action_name in search_path
            if action_name in domain.action_names
        )

        return next(found_actions, f"{UTTER_PREFIX}ask_{slot_name}")

    async def _ask_for_slot(
        self,
        domain: Domain,
        nlg: NaturalLanguageGenerator,
        output_channel: OutputChannel,
        slot_name: Text,
        tracker: DialogueStateTracker,
    ) -> List[Event]:
        logger.debug(f"Request next slot '{slot_name}'")

        action_to_ask_for_next_slot = action.action_from_name(
            self._name_of_utterance(domain, slot_name), None, domain.user_actions
        )
        events_to_ask_for_next_slot = await action_to_ask_for_next_slot.run(
            output_channel, nlg, tracker, domain
        )
        return events_to_ask_for_next_slot

    # helpers
    @staticmethod
    def _to_list(x: Optional[Any]) -> List[Any]:
        """Convert object to a list if it is not a list, `None` converted to empty list.
        """
        if x is None:
            x = []
        elif not isinstance(x, list):
            x = [x]

        return x

    def _list_intents(
        self,
        intent: Optional[Union[Text, List[Text]]] = None,
        not_intent: Optional[Union[Text, List[Text]]] = None,
    ) -> Tuple[List[Text], List[Text]]:
        """Check provided intent and not_intent"""
        if intent and not_intent:
            raise ValueError(
                f"Providing  both intent '{intent}' and not_intent '{not_intent}' "
                f"is not supported."
            )

        return self._to_list(intent), self._to_list(not_intent)

    async def _validate_if_required(
        self,
        tracker: "DialogueStateTracker",
        domain: Domain,
        output_channel: OutputChannel,
        nlg: NaturalLanguageGenerator,
    ) -> List[Event]:
        """Return a list of events from `self.validate(...)`.

         Validation is required if:
            - the form is active
            - the form is called after `action_listen`
            - form validation was not cancelled
        """
        # no active_loop means that it is called during activation
        need_validation = not tracker.active_loop or (
            tracker.latest_action_name == ACTION_LISTEN_NAME
            and tracker.active_loop.get("validate", True)
        )
        if need_validation:
            logger.debug(f"Validating user input '{tracker.latest_message}'.")
            return await self.validate(tracker, domain, output_channel, nlg)

        logger.debug("Skipping validation.")
        return []

    @staticmethod
    def _should_request_slot(tracker: "DialogueStateTracker", slot_name: Text) -> bool:
        """Check whether form action should request given slot"""

        return tracker.get_slot(slot_name) is None

    async def activate(
        self,
        output_channel: "OutputChannel",
        nlg: "NaturalLanguageGenerator",
        tracker: "DialogueStateTracker",
        domain: "Domain",
    ) -> List[Event]:
        """Activate form if the form is called for the first time.

        If activating, validate any required slots that were filled before
        form activation and return `Form` event with the name of the form, as well
        as any `SlotSet` events from validation of pre-filled slots.

        Args:
            output_channel: The output channel which can be used to send messages
                to the user.
            nlg: `NaturalLanguageGenerator` to use for response generation.
            tracker: Current conversation tracker of the user.
            domain: Current model domain.

        Returns:
            Events from the activation.
        """

        logger.debug(f"Activated the form '{self.name()}'.")
        # collect values of required slots filled before activation
        prefilled_slots = {}

        for slot_name in self.required_slots(domain):
            if not self._should_request_slot(tracker, slot_name):
                prefilled_slots[slot_name] = tracker.get_slot(slot_name)

        if not prefilled_slots:
            logger.debug("No pre-filled required slots to validate.")
            return []

        logger.debug(f"Validating pre-filled required slots: {prefilled_slots}")
        return await self.validate_slots(
            prefilled_slots, tracker, domain, output_channel, nlg
        )

    async def do(
        self,
        output_channel: "OutputChannel",
        nlg: "NaturalLanguageGenerator",
        tracker: "DialogueStateTracker",
        domain: "Domain",
        events_so_far: List[Event],
    ) -> List[Event]:
        events = await self._validate_if_required(tracker, domain, output_channel, nlg)

        events += await self.request_next_slot(
            tracker, domain, output_channel, nlg, events_so_far + events
        )

        return events

    async def is_done(
        self,
        output_channel: "OutputChannel",
        nlg: "NaturalLanguageGenerator",
        tracker: "DialogueStateTracker",
        domain: "Domain",
        events_so_far: List[Event],
    ) -> bool:
        return SlotSet(REQUESTED_SLOT, None) in events_so_far

    async def deactivate(self, *args: Any, **kwargs: Any) -> List[Event]:
        logger.debug(f"Deactivating the form '{self.name()}'")
        return []

    def _get_entity_type_of_slot_to_fill(
        self, slot_to_fill: Text, domain: "Domain"
    ) -> Optional[Text]:
        if not slot_to_fill:
            return None

        mappings = self.get_mappings_for_slot(slot_to_fill, domain)
        mappings = [
            m for m in mappings if m.get("type") == str(SlotMapping.FROM_ENTITY)
        ]

        if not mappings:
            return None

        entity_type = mappings[0].get("entity")

        for i in range(1, len(mappings)):
            if entity_type != mappings[i].get("entity"):
                return None

        return entity_type
