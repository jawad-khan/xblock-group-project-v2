from collections import namedtuple
import json
import logging
from xml.etree import ElementTree

from django.utils import html
from lazy.lazy import lazy
import webob
from xblock.core import XBlock
from xblock.fields import String, Boolean, Scope, UNIQUE_ID
from xblock.fragment import Fragment
from xblock.validation import ValidationMessage
from xblockutils.studio_editable import StudioEditableXBlockMixin

from group_project_v2.api_error import ApiError
from group_project_v2.mixins import WorkgroupAwareXBlockMixin, XBlockWithPreviewMixin, NoStudioEditableSettingsMixin
from group_project_v2.project_api import ProjectAPIXBlockMixin
from group_project_v2.project_navigator import ResourcesViewXBlock, SubmissionsViewXBlock
from group_project_v2.upload_file import UploadFile
from group_project_v2.utils import get_link_to_block, FieldValuesContextManager, MUST_BE_OVERRIDDEN
from group_project_v2.utils import (
    outer_html, gettext as _, loader, format_date, build_date_field, mean,
    outsider_disallowed_protected_view
)

log = logging.getLogger(__name__)


class BaseStageComponentXBlock(XBlock):
    @lazy
    def stage(self):
        return self.get_parent()


class BaseGroupProjectResourceXBlock(BaseStageComponentXBlock, StudioEditableXBlockMixin, XBlockWithPreviewMixin):
    display_name = String(
        display_name=_(u"Display Name"),
        help=_(U"This is a name of the resource"),
        scope=Scope.settings,
        default="Group Project V2 Resource"
    )

    description = String(
        display_name=_(u"Resource Description"),
        scope=Scope.settings
    )

    editable_fields = ('display_name', 'description')

    def student_view(self, context):  # pylint: disable=unused-argument, no-self-use
        return Fragment()

    def resources_view(self, context):
        fragment = Fragment()
        render_context = {'resource': self}
        render_context.update(context)
        fragment.add_content(loader.render_template(self.PROJECT_NAVIGATOR_VIEW_TEMPLATE, render_context))
        return fragment


class GroupProjectResourceXBlock(BaseGroupProjectResourceXBlock):
    CATEGORY = "gp-v2-resource"
    STUDIO_LABEL = _(u"Resource")

    PROJECT_NAVIGATOR_VIEW_TEMPLATE = 'templates/html/components/resource.html'

    resource_location = String(
        display_name=_(u"Resource location"),
        help=_(u"A url to download/view the resource"),
        scope=Scope.settings,
    )

    grading_criteria = Boolean(
        display_name=_(u"Grading criteria?"),
        help=_(u"If true, resource will be treated as grading criteria"),
        scope=Scope.settings,
        default=False
    )

    editable_fields = ('display_name', 'description', 'resource_location', 'grading_criteria')

    def author_view(self, context):
        return self.resources_view(context)


class GroupProjectVideoResourceXBlock(BaseGroupProjectResourceXBlock):
    CATEGORY = "gp-v2-video-resource"
    STUDIO_LABEL = _(u"Video Resource")
    PROJECT_NAVIGATOR_VIEW_TEMPLATE = 'templates/html/components/video_resource.html'

    video_id = String(
        display_name=_(u"Ooyala content ID"),
        help=_(u"This is the Ooyala Content Identifier"),
        default="Q1eXg5NzpKqUUzBm5WTIb6bXuiWHrRMi",
        scope=Scope.content,
    )

    editable_fields = ('display_name', 'description', 'video_id')

    @classmethod
    def is_available(cls):
        return True  # TODO: restore conditional availability when switched to use actual Ooyala XBlock

    def resources_view(self, context):
        render_context = {'video_id': self.video_id}
        render_context.update(context)
        fragment = super(GroupProjectVideoResourceXBlock, self).resources_view(render_context)
        return fragment

    def author_view(self, context):
        return self.resources_view(context)

    def validate_field_data(self, validation, data):
        if not data.video_id:
            validation.add(ValidationMessage(
                ValidationMessage.ERROR,
                _(u"Video Resource Block must contain Ooyala content ID")
            ))

        return validation


class StaticContentBaseXBlock(BaseStageComponentXBlock, XBlockWithPreviewMixin, NoStudioEditableSettingsMixin):
    TARGET_PROJECT_NAVIGATOR_VIEW = None
    TEXT_TEMPLATE = None
    TEMPLATE_PATH = "templates/html/components/static_content.html"

    def student_view(self, context):
        try:
            activity = self.stage.activity
            target_block = activity.project.navigator.get_child_of_category(self.TARGET_PROJECT_NAVIGATOR_VIEW)
        except AttributeError:
            activity = None
            target_block = None

        if target_block is None:
            return Fragment()

        render_context = {
            'block': self,
            'block_link': get_link_to_block(target_block),
            'block_text': self.TEXT_TEMPLATE.format(activity_name=activity.display_name),
            'target_block_id': str(target_block.scope_ids.usage_id),
            'view_icon': target_block.icon
        }
        render_context.update(context)

        fragment = Fragment()
        fragment.add_content(loader.render_template(self.TEMPLATE_PATH, render_context))
        return fragment


class SubmissionsStaticContentXBlock(StaticContentBaseXBlock):
    DISPLAY_NAME = _(u"Submissions Help Text")
    STUDIO_LABEL = DISPLAY_NAME
    CATEGORY = "gp-v2-static-submissions"

    display_name_with_default = DISPLAY_NAME

    TARGET_PROJECT_NAVIGATOR_VIEW = SubmissionsViewXBlock.CATEGORY
    TEXT_TEMPLATE = "You can upload (or replace) your file(s) before the due date in the project navigator panel" \
                    " at right by clicking the upload button"


class GradeRubricStaticContentXBlock(StaticContentBaseXBlock):
    DISPLAY_NAME = _(u"Grade Rubric Help Text")
    STUDIO_LABEL = DISPLAY_NAME
    CATEGORY = "gp-v2-static-grade-rubric"

    display_name_with_default = DISPLAY_NAME

    TARGET_PROJECT_NAVIGATOR_VIEW = ResourcesViewXBlock.CATEGORY
    TEXT_TEMPLATE = "The {activity_name} grading rubric is provided in the project navigator panel" \
                    " at right by clicking the resources button"""


# pylint: disable=invalid-name
SubmissionUpload = namedtuple("SubmissionUpload", "location file_name submission_date user_details")


@XBlock.needs('user')
@XBlock.wants('notifications')
class GroupProjectSubmissionXBlock(
    BaseStageComponentXBlock, ProjectAPIXBlockMixin, StudioEditableXBlockMixin, XBlockWithPreviewMixin
):
    CATEGORY = "gp-v2-submission"
    STUDIO_LABEL = _(u"Submission")
    PROJECT_NAVIGATOR_VIEW_TEMPLATE = 'templates/html/components/submission_navigator_view.html'
    REVIEW_VIEW_TEMPLATE = 'templates/html/components/submission_review_view.html'

    display_name = String(
        display_name=_(u"Display Name"),
        help=_(U"This is a name of the submission"),
        scope=Scope.settings,
        default="Group Project V2 Submission"
    )

    description = String(
        display_name=_(u"Resource Description"),
        scope=Scope.settings
    )

    upload_id = String(
        display_name=_(u"Upload ID"),
        help=_(U"This string is used as an identifier for an upload. "
               U"Submissions sharing the same Upload ID will be updated simultaneously"),
    )

    editable_fields = ('display_name', 'description', 'upload_id')

    STAGE_NOT_OPEN_TEMPLATE = _(u"Can't {action} as stage is not yet opened.")
    STAGE_CLOSED_TEMPLATE = _(u"Can't {action} as stage is closed.")

    SUCCESSFUL_UPLOAD_TITLE = _(u"Upload complete.")
    FAILED_UPLOAD_TITLE = _(u"Upload failed.")
    SUCCESSFUL_UPLOAD_MESSAGE_TPL = _(
        u"Your deliverable have been successfully uploaded. You can attach an updated version of the "
        u"deliverable by clicking the <span class='icon {icon}'></span> icon at any time before the deadline passes."
    )
    FAILED_UPLOAD_MESSAGE_TPL = _(u"Error uploading file: {error_goes_here}")

    SUBMISSION_RECEIVED_EVENT = "activity.received_submission"

    def get_upload(self, group_id):
        submission_map = self.project_api.get_latest_workgroup_submissions_by_id(group_id)
        submission_data = submission_map.get(self.upload_id, None)

        if submission_data is None:
            return None

        return SubmissionUpload(
            submission_data["document_url"],
            submission_data["document_filename"],
            format_date(build_date_field(submission_data["modified"])),
            submission_data.get("user_details", None)
        )

    @property
    def upload(self):
        return self.get_upload(self.stage.activity.workgroup["id"])

    def student_view(self, context):  # pylint: disable=unused-argument, no-self-use
        return Fragment()

    def submissions_view(self, context):
        fragment = Fragment()
        uploading_allowed = self.stage.available_now and self.stage.is_group_member
        render_context = {'submission': self, 'upload': self.upload, 'disabled': not uploading_allowed}
        render_context.update(context)
        fragment.add_content(loader.render_template(self.PROJECT_NAVIGATOR_VIEW_TEMPLATE, render_context))
        fragment.add_javascript_url(self.runtime.local_resource_url(self, 'public/js/components/submission.js'))
        fragment.initialize_js("GroupProjectSubmissionBlock")
        return fragment

    def submission_review_view(self, context):
        group_id = context.get('group_id', self.stage.activity.workgroup["id"])
        fragment = Fragment()
        render_context = {'submission': self, 'upload': self.get_upload(group_id)}
        render_context.update(context)
        fragment.add_content(loader.render_template(self.REVIEW_VIEW_TEMPLATE, render_context))
        # NOTE: adding js/css likely won't work here, as the result of this view is added as an HTML to an existing DOM
        # element
        return fragment

    @XBlock.handler
    def upload_submission(self, request, suffix=''):  # pylint: disable=unused-argument
        """
        Handles submission upload and marks stage as completed if all submissions in stage have uploads.
        """
        if not self.stage.available_now:
            template = self.STAGE_NOT_OPEN_TEMPLATE if not self.stage.is_open else self.STAGE_CLOSED_TEMPLATE
            response_data = {'result': 'error', 'message': template.format(action=self.stage.STAGE_ACTION)}
            failure_code = 422  # 422 = unprocessable entity

        elif not self.stage.is_group_member:
            response_data = {'result': 'error', 'message': _(u"Only group members can upload files")}
            failure_code = 403  # 403 - forbidden

        else:
            target_activity = self.stage.activity
            response_data = {
                "title": self.SUCCESSFUL_UPLOAD_TITLE,
                "message": self.SUCCESSFUL_UPLOAD_MESSAGE_TPL.format(icon='fa fa-paperclip')
            }
            failure_code = 0
            try:
                context = {
                    "user_id": target_activity.user_id,
                    "group_id": target_activity.workgroup['id'],
                    "project_api": self.project_api,
                    "course_id": target_activity.course_id
                }

                uploaded_file = self.persist_and_submit_file(
                    target_activity, context, request.params[self.upload_id].file
                )

                response_data["submissions"] = {uploaded_file.submission_id: uploaded_file.file_url}

                self.stage.check_submissions_and_mark_complete()
                response_data["new_stage_states"] = [self.stage.get_new_stage_state_data()]

            except Exception as exception:  # pylint: disable=broad-except
                log.exception(exception)
                failure_code = 500
                if isinstance(exception, ApiError):
                    failure_code = exception.code
                error_message = getattr(exception, "message", _(u"Unknown error"))

                response_data.update({
                    "title": self.FAILED_UPLOAD_TITLE,
                    "message": self.FAILED_UPLOAD_MESSAGE_TPL.format(error_goes_here=error_message)
                })

        response = webob.response.Response(body=json.dumps(response_data))
        if failure_code:
            response.status_code = failure_code

        return response

    def persist_and_submit_file(self, activity, context, file_stream):
        """
        Saves uploaded files to their permanent location, sends them to submissions backend and emits submission events
        """
        uploaded_file = UploadFile(file_stream, self.upload_id, context)

        # Save the files first
        try:
            uploaded_file.save_file()
        except Exception as save_file_error:  # pylint: disable=broad-except
            original_message = save_file_error.message if hasattr(save_file_error, "message") else ""
            save_file_error.message = _("Error storing file {} - {}").format(uploaded_file.file.name, original_message)
            raise

        # It have been saved... note the submission
        try:
            uploaded_file.submit()
            # Emit analytics event...
            self.runtime.publish(
                self,
                self.SUBMISSION_RECEIVED_EVENT,
                {
                    "submission_id": uploaded_file.submission_id,
                    "filename": uploaded_file.file.name,
                    "content_id": activity.content_id,
                    "group_id": activity.workgroup['id'],
                    "user_id": activity.user_id,
                }
            )
        except Exception as save_record_error:  # pylint: disable=broad-except
            original_message = save_record_error.message if hasattr(save_record_error, "message") else ""
            save_record_error.message = _("Error recording file information {} - {}").format(
                uploaded_file.file.name, original_message
            )
            raise

        # See if the xBlock Notification Service is available, and - if so -
        # dispatch a notification to the entire workgroup that a file has been uploaded
        # Note that the NotificationService can be disabled, so it might not be available
        # in the list of services
        notifications_service = self.runtime.service(self, 'notifications')
        if notifications_service:
            activity.fire_file_upload_notification(notifications_service)

        return uploaded_file


class PeerSelectorXBlock(BaseStageComponentXBlock, XBlockWithPreviewMixin, NoStudioEditableSettingsMixin):
    CATEGORY = "gp-v2-peer-selector"
    STUDIO_LABEL = _(u"Teammate selector")
    display_name_with_default = _(u"Teammate selector XBlock")
    STUDENT_TEMPLATE = "templates/html/components/peer_selector.html"

    @property
    def peers(self):
        return self.stage.team_members

    def student_view(self, context):
        fragment = Fragment()
        render_context = {'selector': self, 'peers': self.peers}
        render_context.update(context)
        fragment.add_css_url(self.runtime.local_resource_url(self, "public/css/components/review_subject_selector.css"))
        fragment.add_content(loader.render_template(self.STUDENT_TEMPLATE, render_context))
        return fragment

    def author_view(self, context):
        fake_peers = [
            {"id": 1, "username": "Jack"},
            {"id": 2, "username": "Jill"},
        ]
        render_context = {
            'demo': True,
            'peers': fake_peers
        }
        render_context.update(context)
        return self.student_view(render_context)


class GroupSelectorXBlock(BaseStageComponentXBlock, XBlockWithPreviewMixin, NoStudioEditableSettingsMixin):
    CATEGORY = "gp-v2-group-selector"
    STUDIO_LABEL = _(u"Group selector")
    display_name_with_default = _(u"Group selector XBlock")
    STUDENT_TEMPLATE = "templates/html/components/group_selector.html"

    @property
    def groups(self):
        return self.stage.review_groups

    def student_view(self, context):
        fragment = Fragment()
        render_context = {'selector': self, 'groups': self.groups}
        render_context.update(context)
        fragment.add_css_url(self.runtime.local_resource_url(self, "public/css/components/review_subject_selector.css"))
        fragment.add_content(loader.render_template(self.STUDENT_TEMPLATE, render_context))
        return fragment

    def author_view(self, context):
        fake_groups = [
            {"id": 1},
            {"id": 2},
        ]
        render_context = {
            'demo': True,
            'groups': fake_groups
        }
        render_context.update(context)
        return self.student_view(render_context)


class GroupProjectReviewQuestionXBlock(BaseStageComponentXBlock, StudioEditableXBlockMixin, XBlockWithPreviewMixin):
    CATEGORY = "gp-v2-review-question"
    STUDIO_LABEL = _(u"Review Question")

    @property
    def display_name_with_default(self):
        return self.title or _(u"Review Question")

    question_id = String(
        display_name=_(u"Question ID"),
        default=UNIQUE_ID,
        scope=Scope.content
    )

    title = String(
        display_name=_(u"Question text"),
        default="",
        scope=Scope.content
    )

    # Label could be an HTML child XBlock, content could be a XBlock encapsulating HTML input/select/textarea
    # unfortunately, there aren't any XBlocks for HTML controls, hence reusing GP V1 approach
    assessment_title = String(
        display_name=_(u"Assessment question text"),
        help=_(u"Overrides question title when displayed in assessment mode"),
        default=None,
        scope=Scope.content
    )

    question_content = String(
        display_name=_(u"Question content"),
        help=_(u"HTML control"),
        default="",
        scope=Scope.content,
        multiline_editor="xml",
        xml_node=True
    )

    required = Boolean(
        display_name=_(u"Required"),
        default=False,
        scope=Scope.content
    )

    grade = Boolean(
        display_name=_(u"Grading"),
        help=_(u"IF True, answers to this question will be used to calculate student grade for Group Project."),
        default=False,
        scope=Scope.content
    )

    single_line = Boolean(
        display_name=_(u"Single line"),
        help=_(u"If True question label and content will be displayed on single line, allowing for more compact layout."
               u"Only affects presentation."),
        default=False,
        scope=Scope.content
    )

    question_css_classes = String(
        display_name=_(u"CSS classes"),
        help=_(u"CSS classes to be set on question element. Only affects presentation."),
        scope=Scope.content
    )

    editable_fields = (
        "question_id", "title", "assessment_title", "question_content", "required", "grade", "single_line",
        "question_css_classes"
    )
    has_author_view = True

    @lazy
    def stage(self):
        return self.get_parent()

    def render_content(self):
        try:
            answer_node = ElementTree.fromstring(self.question_content)
        except ElementTree.ParseError:
            message_tpl = "Exception when parsing question content for question {question_id}. Content is [{content}]."
            message_tpl.format(question_id=self.question_id, content=self.question_content)
            log.exception(message_tpl)
            return ""

        answer_node.set('name', self.question_id)
        answer_node.set('id', self.question_id)
        current_class = answer_node.get('class')
        answer_classes = ['answer']
        if current_class:
            answer_classes.append(current_class)
        if self.single_line:
            answer_classes.append('side')
        if self.stage.is_closed:
            answer_node.set('disabled', 'disabled')
        else:
            answer_classes.append('editable')
        answer_node.set('class', ' '.join(answer_classes))

        return outer_html(answer_node)

    def student_view(self, context):
        question_classes = ["question"]
        if self.required:
            question_classes.append("required")
        if self.question_css_classes:
            question_classes.append(self.question_css_classes)

        fragment = Fragment()
        render_context = {
            'question': self,
            'question_classes': " ".join(question_classes),
            'question_content': self.render_content()
        }
        render_context.update(context)
        fragment.add_content(loader.render_template("templates/html/components/review_question.html", render_context))
        return fragment

    def studio_view(self, context):
        fragment = super(GroupProjectReviewQuestionXBlock, self).studio_view(context)

        # TODO: StudioEditableXBlockMixin should really support Codemirror XML editor
        fragment.add_css_url(self.runtime.local_resource_url(self, "public/css/components/question_edit.css"))
        fragment.add_javascript_url(self.runtime.local_resource_url(self, "public/js/components/question_edit.js"))
        fragment.initialize_js("GroupProjectQuestionEdit")
        return fragment

    def author_view(self, context):
        fragment = self.student_view(context)
        fragment.add_css_url(self.runtime.local_resource_url(self, "public/css/components/question_edit.css"))
        return fragment


class GroupProjectBaseFeedbackDisplayXBlock(
    BaseStageComponentXBlock, StudioEditableXBlockMixin, XBlockWithPreviewMixin, WorkgroupAwareXBlockMixin
):
    DEFAULT_QUESTION_ID_VALUE = None

    question_id = String(
        display_name=_(u"Question"),
        help=_(u"Question to be assessed"),
        scope=Scope.content,
        default=DEFAULT_QUESTION_ID_VALUE
    )

    show_mean = Boolean(
        display_name=_(u"Show mean value"),
        help=_(u"If True, converts review answers to numbers and calculates mean value"),
        default=False,
        scope=Scope.content
    )

    editable_fields = ("question_id", "show_mean")
    has_author_view = True

    @property
    def activity_questions(self):
        raise NotImplementedError(MUST_BE_OVERRIDDEN)

    @property
    def display_name_with_default(self):
        if self.question:
            return _(u'Review Assessment for question "{question_title}"').format(question_title=self.question.title)
        else:
            return _(u"Review Assessment")

    @lazy
    def question(self):
        matching_questions = [
            question for question in self.activity_questions if question.question_id == self.question_id
        ]
        if len(matching_questions) > 1:
            raise ValueError("Question ID is not unique")
        if not matching_questions:
            return None

        return matching_questions[0]

    @outsider_disallowed_protected_view
    def student_view(self, context):
        if self.question is None:
            raise ValueError("No question selected")

        raw_feedback = self.get_feedback()

        feedback = []
        for item in raw_feedback:
            feedback.append(html.escape(item['answer']))

        fragment = Fragment()
        title = self.question.assessment_title if self.question.assessment_title else self.question.title
        render_context = {'assessment': self, 'question_title': title, 'feedback': feedback}
        if self.show_mean:
            try:
                render_context['mean'] = "{0:.1f}".format(mean(feedback))
            except ValueError as exc:
                log.warn(exc)
                render_context['mean'] = _(u"N/A")

        render_context.update(context)
        fragment.add_content(loader.render_template("templates/html/components/review_assessment.html", render_context))
        return fragment

    def validate(self):
        validation = super(GroupProjectBaseFeedbackDisplayXBlock, self).validate()

        if not self.question_id:
            validation.add(ValidationMessage(
                ValidationMessage.ERROR,
                _(u"No question selected")
            ))

        if self.question is None:
            validation.add(ValidationMessage(
                ValidationMessage.ERROR,
                _(u"Selected question not found")
            ))

        return validation

    def author_view(self, context):
        if self.question:
            return self.student_view(context)

        fragment = Fragment()
        fragment.add_content(_(u"Question is not selected"))
        return fragment

    def studio_view(self, context):
        # can't use values_provider as we need it to be bound to current block instance
        with FieldValuesContextManager(self, 'question_id', self.question_ids_values_provider):
            return super(GroupProjectBaseFeedbackDisplayXBlock, self).studio_view(context)

    def question_ids_values_provider(self):
        not_selected = {
            "display_name": _(u"--- Not selected ---"), "value": self.DEFAULT_QUESTION_ID_VALUE
        }
        question_values = [
            {"display_name": question.title, "value": question.question_id}
            for question in self.activity_questions
        ]
        return [not_selected] + question_values


class GroupProjectTeamEvaluationDisplayXBlock(GroupProjectBaseFeedbackDisplayXBlock):
    CATEGORY = "gp-v2-peer-assessment"
    STUDIO_LABEL = _(u"Team Evaluation Display")

    @property
    def activity_questions(self):
        return self.stage.activity.team_evaluation_questions

    def get_feedback(self):
        all_feedback = self.project_api.get_user_peer_review_items(
            self.user_id,
            self.group_id,
            self.stage.content_id,
        )

        return [item for item in all_feedback if item["question"] == self.question_id]


class GroupProjectGradeEvaluationDisplayXBlock(GroupProjectBaseFeedbackDisplayXBlock):
    CATEGORY = "gp-v2-group-assessment"
    STUDIO_LABEL = _(u"Grade Evaluation Display")

    @property
    def activity_questions(self):
        return self.stage.activity.peer_review_questions

    def get_feedback(self):
        all_feedback = self.project_api.get_workgroup_review_items_for_group(
            self.group_id,
            self.stage.content_id,
        )
        return [item for item in all_feedback if item["question"] == self.question_id]


class ProjectTeamXBlock(
    BaseStageComponentXBlock, XBlockWithPreviewMixin, NoStudioEditableSettingsMixin, StudioEditableXBlockMixin
):
    CATEGORY = 'gp-v2-project-team'
    STUDIO_LABEL = _(u"Project Team")

    display_name_with_default = STUDIO_LABEL

    def student_view(self, context):
        fragment = Fragment()
        render_context = {
            'team_members': self.stage.team_members,
            'course_id': self.stage.course_id,
            'group_id': self.stage.workgroup['id']
        }
        render_context.update(context)

        fragment.add_content(loader.render_template("templates/html/components/project_team.html", render_context))
        fragment.add_css_url(self.runtime.local_resource_url(self, "public/css/components/project_team.css"))
        fragment.add_javascript_url(self.runtime.local_resource_url(self, "public/js/components/project_team.js"))
        fragment.initialize_js("ProjectTeamXBlock")
        return fragment
