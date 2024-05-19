"""
The base directive module.
"""

import asyncio
import re
import contextlib
from typing import Any
from abc import ABC

from lionagi.libs.ln_parse import ParseUtil, StringMatch
from lionagi.core.collections.abc import ActionError
from lionagi.core.message import Instruction, ActionRequest, ActionResponse
from lionagi.core.message.util import _parse_action_request
from lionagi.core.validator.validator import Validator
from lionagi.core.unit.util import process_tools


class DirectiveMixin(ABC):
    """
    DirectiveMixin is a class for handling chat operations and processing
    responses.
    """

    async def _base_chat(
        self,
        instruction=None,
        *,
        system=None,
        context=None,
        sender=None,
        recipient=None,
        requested_fields=None,
        form=None,
        tools=False,
        invoke_tool=True,
        return_form=True,
        strict=False,
        rulebook=None,
        imodel=None,
        use_annotation=True,
        branch=None,
        clear_messages=False,
        return_branch=False,
        **kwargs,
    ):
        """
        Handles the base chat operation by configuring the chat and processing
        the response.
        """
        branch = branch or self.branch
        if clear_messages:
            branch.clear()
            branch.set_system(system)

        config = self._create_chat_config(
            system=system,
            instruction=instruction,
            context=context,
            sender=sender,
            recipient=recipient,
            requested_fields=requested_fields,
            form=form,
            tools=tools,
            branch=branch,
            **kwargs,
        )

        payload, completion = await self._call_chatcompletion(
            imodel=imodel, branch=branch, **config
        )

        out_ = await self._output(
            payload=payload,
            completion=completion,
            sender=sender,
            invoke_tool=invoke_tool,
            requested_fields=requested_fields,
            form=form,
            return_form=return_form,
            strict=strict,
            rulebook=rulebook,
            use_annotation=use_annotation,
        )

        return out_, branch if return_branch else out_

    def _create_chat_config(
        self,
        system=None,
        instruction=None,
        context=None,
        sender=None,
        recipient=None,
        requested_fields=None,
        form=None,
        tools=False,
        branch=None,
        **kwargs,
    ) -> Any:
        """
        Creates the chat configuration based on the provided parameters.
        """
        branch = branch or self.branch

        if system:
            branch.add_message(system=system)

        if not form:
            if recipient == "branch.ln_id":
                recipient = branch.ln_id

            branch.add_message(
                instruction=instruction,
                context=context,
                sender=sender,
                recipient=recipient,
                requested_fields=requested_fields,
            )
        else:
            instruct_ = Instruction.from_form(form)
            branch.add_message(instruction=instruct_)

        if "tool_parsed" in kwargs:
            kwargs.pop("tool_parsed")
            tool_kwarg = {"tools": tools}
            kwargs = tool_kwarg | kwargs
        elif tools and branch.has_tools:
            kwargs = branch.tool_manager.parse_tool(tools=tools, **kwargs)

        config = {**self.imodel.config, **kwargs}
        if sender is not None:
            config["sender"] = sender

        return config

    async def _call_chatcompletion(self, imodel=None, branch=None, **kwargs):
        """
        Calls the chat completion model.
        """
        imodel = imodel or self.imodel
        branch = branch or self.branch
        return await imodel.call_chat_completion(branch.to_chat_messages(), **kwargs)

    async def _process_chatcompletion(
        self,
        payload,
        completion,
        sender,
        invoke_tool=True,
        branch=None,
        action_request=None,
    ):
        """
        Processes the chat completion response.
        """
        branch = branch or self.branch
        _msg = None
        if "choices" in completion:
            aa = payload.pop("messages", None)
            branch.update_last_instruction_meta(payload)
            msg = completion.pop("choices", None)
            if msg and isinstance(msg, list):
                msg = msg[0]

            if isinstance(msg, dict):
                _msg = msg.pop("message", None)
                completion.update(msg)

                branch.add_message(
                    assistant_response=_msg,
                    metadata=completion,
                    sender=sender,
                )
                branch.imodel.status_tracker.num_tasks_succeeded += 1
        else:
            branch.imodel.status_tracker.num_tasks_failed += 1

        return await self._process_action_request(
            _msg=_msg,
            branch=branch,
            invoke_tool=invoke_tool,
            action_request=action_request,
        )

    async def _process_action_request(
        self,
        _msg=None,
        branch=None,
        invoke_tool=True,
        action_request=None,
    ):
        """
        Processes an action request from the assistant response.
        """
        action_request = action_request or _parse_action_request(_msg)
        if action_request is None:
            return _msg if _msg else False

        if action_request:
            for i in action_request:
                if i.function in branch.tool_manager.registry:
                    i.recipient = branch.tool_manager.registry[
                        i.function
                    ].ln_id
                else:
                    raise ActionError(f"Tool {i.function} not found in registry")
                branch.add_message(action_request=i, recipient=i.recipient)

        if invoke_tool:
            tasks = []
            for i in action_request:
                tool = branch.tool_manager.registry[i.function]
                tasks.append(asyncio.create_task(tool.invoke(i.arguments)))

            results = await asyncio.gather(*tasks)

            for idx, item in enumerate(results):
                branch.add_message(
                    action_request=action_request[idx],
                    func_outputs=item,
                    sender=action_request[idx].recipient,
                    recipient=action_request[idx].sender,
                )

        return None

    async def _output(
        self,
        payload,
        completion,
        sender,
        invoke_tool,
        requested_fields,
        form=None,
        return_form=True,
        strict=False,
        rulebook=None,
        use_annotation=True,
        template_name=None,
    ) -> Any:
        """
        Outputs the final processed response.
        """
        _msg = await self._process_chatcompletion(
            payload=payload,
            completion=completion,
            sender=sender,
            invoke_tool=invoke_tool,
        )

        if _msg is None:
            return None

        response_ = self._process_model_response(_msg, requested_fields)

        if form:
            validator = Validator(rulebook=rulebook) if rulebook else self.validator
            form = await validator.validate_response(
                form=form,
                response=response_,
                strict=strict,
                use_annotation=use_annotation,
            )
            if template_name:
                form.template_name = template_name

            return (
                form
                if return_form
                else {
                    i: form.work_fields[i]
                    for i in form.requested_fields
                    if form.work_fields[i] is not None
                }
            )

        return response_

    @staticmethod
    def _process_model_response(content_, requested_fields):
        """
        Processes the model response content.
        """
        out_ = content_.get("content", "")

        if requested_fields:
            with contextlib.suppress(Exception):
                return StringMatch.force_validate_dict(out_, requested_fields)

        if isinstance(out_, str):
            with contextlib.suppress(Exception):
                match = re.search(r"```json\n({.*?})\n```", out_, re.DOTALL)
                if match:
                    out_ = ParseUtil.fuzzy_parse_json(match.group(1))

        return out_ or content_

    async def _chat(
        self,
        instruction=None,
        context=None,
        system=None,
        sender=None,
        recipient=None,
        branch=None,
        requested_fields=None,
        form=None,
        tools=False,
        invoke_tool=True,
        return_form=True,
        strict=False,
        rulebook=None,
        imodel=None,
        clear_messages=False,
        use_annotation=True,
        timeout: float = None,
        return_branch=False,
        **kwargs,
    ):
        a = await self._base_chat(
            context=context,
            instruction=instruction,
            system=system,
            sender=sender,
            recipient=recipient,
            requested_fields=requested_fields,
            form=form,
            tools=tools,
            invoke_tool=invoke_tool,
            return_form=return_form,
            strict=strict,
            rulebook=rulebook,
            imodel=imodel,
            use_annotation=use_annotation,
            timeout=timeout,
            branch=branch,
            clear_messages=clear_messages,
            return_branch=return_branch,
            **kwargs,
        )

        a = list(a)
        if len(a) == 2 and a[0] == a[1]:
            return a[0] if not isinstance(a[0], tuple) else a[0][0]

        return a[0], a[1]


    async def _direct(
        self,
        instruction=None,
        context=None,
        form=None,
        branch=None,
        tools=None,
        reason: bool = None,
        predict: bool = None,
        score: bool=None,
        select: bool=None,
        plan: bool = None,
        allow_action: bool = None,
        allow_extension: bool = None,
        confidence: bool = None,
        max_extension: int = None,
        score_num_digits=None,
        score_range=None,
        select_choices=None,
        plan_num_step=None,
        predict_num_sentences=None,
        clear_messages=False,
        return_branch=False,
        **kwargs,
    ):
        a = await self._base_direct(
            instruction=instruction,
            context=context,
            form=form,
            branch=branch,
            tools=tools,
            reason=reason,
            predict=predict,
            score=score,
            select=select,
            plan=plan,
            allow_action=allow_action,
            allow_extension=allow_extension,
            confidence=confidence,
            max_extension=max_extension,
            score_num_digits=score_num_digits,
            score_range=score_range,
            select_choices=select_choices,
            plan_num_step=plan_num_step,
            predict_num_sentences=predict_num_sentences,
            clear_messages=clear_messages,
            return_branch=return_branch,
            **kwargs,
        )

        a = list(a)
        if len(a) == 2 and a[0] == a[1]:
            return a[0] if not isinstance(a[0], tuple) else a[0][0]

        return a[0], a[1]


    async def _base_direct(
        self,
        instruction=None,
        *,
        context=None,
        form=None,
        branch=None,
        tools=None,
        reason: bool = None,
        predict: bool = None,
        score: bool=None,
        select: bool=None,
        plan: bool = None,
        allow_action: bool = None,
        allow_extension: bool = None,
        confidence: bool = None,
        max_extension: int = None,
        score_num_digits=None,
        score_range=None,
        select_choices=None,
        plan_num_step=None,
        predict_num_sentences=None,
        clear_messages=False,
        return_branch=False,
        **kwargs,
    ):
        
        # Ensure branch is initialized
        branch = branch or self.branch
        if clear_messages:
            branch.clear()

        # Set a default max_extension if allow_extension is True and max_extension is None
        if allow_extension and not max_extension:
            max_extension = 3  # Set a default limit for recursion
        
        if not form:
            form = self.form_template(
                instruction=instruction,
                context=context,
                reason=reason,
                predict=predict,
                score=score,
                select=select,
                plan=plan,
                allow_action=allow_action,
                allow_extension=allow_extension,
                max_extension=max_extension,
                confidence=confidence,
                score_num_digits=score_num_digits,
                score_range=score_range,
                select_choices=select_choices,
                plan_num_step=plan_num_step,
                predict_num_sentences=predict_num_sentences,
            )

        # Process tools if provided
        if tools:
            process_tools(tools, branch)

        # Call the base chat method
        form = await self._chat(
            form=form,
            branch=branch,
            tools=tools,
            **kwargs,
        )

        # Handle actions if allowed and required
        if allow_action and getattr(form, "action_required", None):
            actions = getattr(form, "actions", None)
            if actions:
                form = await self._act(form, branch, actions=actions)

        last_form = form

        # Handle extensions if allowed and required
        extension_forms = []
        while (
            allow_extension and
            getattr(last_form, "extension_required", None) and 
            getattr(last_form, "answer", None) == "PLEASE_EXTEND"
        ):
            if max_extension <= 0:
                break
            max_extension -= 1
            
            last_form = await self._extend(
                form = last_form,
                tools = tools,
                reason = reason,
                predict = predict,
                score = score,
                select = select,
                plan = getattr(last_form, "plan", None),
                allow_action=allow_action,
                confidence=confidence,
                score_num_digits=score_num_digits,
                score_range=score_range,
                select_choices=select_choices,
                predict_num_sentences=predict_num_sentences,
                allow_extension=isinstance(max_extension, int) and max_extension > 0,
                max_extension=max_extension,
                **kwargs,
            )
            
            extension_forms.extend([last_form])
            last_form = last_form[0] if last_form else None

        if extension_forms:
            if not getattr(form, "extension_forms", None):
                form._add_field("extension_forms", list, None, [])
            form.extension_forms.extend(extension_forms)

        if "PLEASE_ACTION" in form.answer:
            answer = await self._chat("please provide final answer basing on the above information, only provide answer field as a string",)
            form.answer = answer.replace('{"answer": "', '').replace('"}', '')

        return form, branch if return_branch else form
 

    async def _extend(
        self, 
        form, 
        tools, 
        reason, 
        predict, 
        score, 
        select, 
        plan, 
        allow_action, 
        confidence, 
        score_num_digits, 
        score_range, 
        select_choices, 
        predict_num_sentences, 
        allow_extension, 
        max_extension, 
        **kwargs
    ):
        extension_forms = []
 
        # Ensure the next step in the plan is handled
        directive_kwargs = {
            "tools": tools,
            "reason": reason,
            "predict": predict,
            "score": score,
            "select": select,
            "allow_action": allow_action,
            "confidence": confidence,
            "score_num_digits": score_num_digits,
            "score_range": score_range,
            "select_choices": select_choices,
            "predict_num_sentences": predict_num_sentences,
            "allow_extension": allow_extension,
            "max_extension": max_extension,
            **kwargs,
        }

        if plan:
            keys = [f"step_{i+1}" for i in range(len(plan))]
            plan = StringMatch.force_validate_dict(plan, keys)
            
            # If plan is provided, process each step
            for i in keys:
                directive_kwargs["instruction"] = plan[i]
                last_form = await self._direct(**directive_kwargs)
                extension_forms.append(last_form)
                directive_kwargs["max_extension"] -= 1
                if not last_form.extension_required:
                    break

        else:
            # Handle single step extension
            directive_kwargs["instruction"] = form.instruction
            last_form = await self._direct(**directive_kwargs)
            extension_forms.append(last_form)

        return extension_forms


    async def _act(self, form, branch, actions=None):
        if actions:

            keys = [f"action_{i+1}" for i in range(len(actions))]
            actions = StringMatch.force_validate_dict(actions, keys)

            try:
                requests = []
                for k in keys:
                    _func = actions[k]["function"]
                    _func = _func.replace("functions.", "")
                    msg = ActionRequest(
                        function=_func,
                        arguments=actions[k]["arguments"],
                        sender=branch.ln_id,
                        recipient=branch.tool_manager.registry[
                            _func
                        ].ln_id,
                    )
                    requests.append(msg)
                    branch.add_message(action_request=msg)

                if requests:
                    out = await self._process_action_request(
                        branch=branch, invoke_tool=True, action_request=requests
                    )

                    if out is False:
                        raise ValueError(
                            "Error processing action request: No requests found."
                        )

                    len_actions = len(actions)
                    action_responses = branch.messages[-len_actions:]

                    if not all(
                        isinstance(i, ActionResponse) for i in action_responses
                    ):
                        raise ValueError(
                            "Error processing action request: Invalid action response."
                        )

                    _action_responses = {}
                    for idx, item in enumerate(action_responses):
                        _action_responses[f"action_{idx+1}"] = item._to_dict()
                        
                    form._add_field(
                        "action_response", dict, None, _action_responses
                    )
                    form.append_to_request("action_response")
                    
                    form._add_field(
                        "action_performed", bool, None, True
                    )
                    form.append_to_request("action_performed")

            except Exception as e:
                raise ValueError(f"Error processing action request: {e}")

        return form


    async def _select(
        self,
        form=None,
        choices=None,
        reason=False,
        confidence_score=None,
        instruction=None,
        template=None,
        context=None,
        branch=None,
        **kwargs,
    ):
        """
        Selects a response based on the provided parameters.

        Args:
            form (Any, optional): Form to create instruction from.
            choices (Any, optional): Choices for the selection.
            reason (bool, optional): Whether to include a reason for the selection.
            confidence_score (Any, optional): Confidence score for the selection.
            instruction (Any, optional): Instruction for the selection.
            template (Any, optional): Template for the selection.
            context (Any, optional): Context to perform the selection on.
            branch (Any, optional): Branch to use for the selection.
            **kwargs: Additional arguments for the selection.

        Returns:
            Any: The selection response.
        """
        branch = branch or self.branch

        if not form:
            form = template(
                choices=choices,
                reason=reason,
                confidence_score=confidence_score,
                instruction=instruction,
                context=context,
            )

        return await self._chat(form=form, return_form=True, branch=branch, **kwargs)

    async def _predict(
        self,
        form=None,
        num_sentences=None,
        reason=False,
        confidence_score=None,
        instruction=None,
        context=None,
        branch=None,
        template=None,
        **kwargs,
    ):
        """
        Predicts a response based on the provided parameters.

        Args:
            form (Any, optional): Form to create instruction from.
            num_sentences (Any, optional): Number of sentences for the prediction.
            reason (bool, optional): Whether to include a reason for the prediction.
            confidence_score (Any, optional): Confidence score for the prediction.
            instruction (Any, optional): Instruction for the prediction.
            context (Any, optional): Context to perform the prediction on.
            branch (Any, optional): Branch to use for the prediction.
            template (Any, optional): Template for the prediction.
            **kwargs: Additional arguments for the prediction.

        Returns:
            Any: The prediction response.
        """
        branch = branch or self.branch

        if not form:
            form = template(
                instruction=instruction,
                context=context,
                num_sentences=num_sentences,
                confidence_score=confidence_score,
                reason=reason,
            )

        return await self._chat(form=form, return_form=True, branch=branch, **kwargs)

    async def _score(
        self,
        form=None,
        score_range=None,
        include_endpoints=None,
        num_digit=None,
        reason=False,
        confidence_score=None,
        instruction=None,
        context=None,
        branch=None,
        template=None,
        **kwargs,
    ):
        
        branch = branch or self.branch
        if not form:
            form = template(
                score_range=score_range,
                include_endpoints=include_endpoints,
                num_digit=num_digit,
                reason=reason,
                confidence_score=confidence_score,
                instruction=instruction,
                context=context,
            )

        return await self._chat(form=form, return_form=True, branch=branch, **kwargs)
    
    async def _plan(
        self,
        form=None,
        num_step=None,
        reason=False,
        confidence_score=None,
        instruction=None,
        context=None,
        branch=None,
        template=None,
        **kwargs,
    ):

        branch = branch or self.branch
        template = template or self.default_template

        if not form:
            form = template(
                instruction=instruction,
                context=context,
                num_step=num_step,
                reason=reason,
                confidence_score=confidence_score,
            )

        return await self._chat(form=form, **kwargs)
