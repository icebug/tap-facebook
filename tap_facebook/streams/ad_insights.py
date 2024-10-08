"""Stream class for AdInsights."""

from __future__ import annotations

import time
from datetime import datetime, timezone
import typing as t
from functools import lru_cache

import facebook_business.adobjects.user as fb_user
import pendulum
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.adreportrun import AdReportRun
from facebook_business.adobjects.adsinsights import AdsInsights
from facebook_business.api import FacebookAdsApi
from singer_sdk import typing as th
from singer_sdk.streams.core import REPLICATION_INCREMENTAL, Stream

SLEEP_TIME_INCREMENT = 5
INSIGHTS_MAX_WAIT_TO_START_SECONDS = 5 * 60
INSIGHTS_MAX_WAIT_TO_FINISH_SECONDS = 30 * 60

REPORT_DEFINITION = {
    "name": "adset",
    "level": "adset",
    "time_increment_days": 1,
    "fields": ["adset_id", "date_start", "date_stop", "impressions", "clicks", "spend"],
    "breakdowns": ["country", "region"],
}


class AdsInsightStream(Stream):
    name = "adsinsights"
    replication_method = REPLICATION_INCREMENTAL
    replication_key = "date_start"

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Initialize the stream."""
        self._report_definition = REPORT_DEFINITION
        kwargs["name"] = f"{self.name}_{self._report_definition['name']}"
        super().__init__(*args, **kwargs)

    @property
    def primary_keys(self) -> list[str] | None:
        return ["date_start", "adset_id"]

    @primary_keys.setter
    def primary_keys(self, new_value: list[str] | None) -> None:
        """Set primary key(s) for the stream.

        Args:
            new_value: TODO
        """
        self._primary_keys = new_value

    @staticmethod
    def _get_datatype(field: str) -> th.Type | None:
        d_type = AdsInsights._field_types[field]  # noqa: SLF001
        if d_type == "string":
            return th.StringType()
        if d_type.startswith("list"):
            return th.ArrayType(th.ObjectType())
        msg = f"Type not found for field: {field}"
        raise RuntimeError(msg)

    @property
    @lru_cache  # noqa: B019
    def schema(self) -> dict:
        properties: th.List[th.Property] = []

        columns = list(AdsInsights.Field.__dict__)[1:]
        fields = (
            self._report_definition["fields"] + self._report_definition["breakdowns"]
        )

        # Use the AdsInsights class to determine the datatype of each field.
        # If the field is not found in the AdsInsights class, default to StringType.
        for field in fields:
            if field in columns:
                dtype = self._get_datatype(field)
            else:
                dtype = th.StringType()
            properties.append(th.Property(field, dtype))

        properties.append(th.Property("extracted_at", th.DateTimeType()))

        return th.PropertiesList(*properties).to_dict()

    def _initialize_client(self) -> None:
        FacebookAdsApi.init(
            access_token=self.config["access_token"],
            timeout=300,
            api_version=self.config["api_version"],
        )
        fb_user.User(fbid="me")

        account_id = self.config["account_id"]
        self.account = AdAccount(f"act_{account_id}").api_get()
        if not self.account:
            msg = f"Couldn't find account with id {account_id}"
            raise RuntimeError(msg)

    def _run_job_to_completion(self, params: dict) -> th.Any:
        job = self.account.get_insights(
            params=params,
            is_async=True,
        )
        status = None
        time_start = time.time()
        while status != "Job Completed":
            duration = time.time() - time_start
            job = job.api_get()
            status = job[AdReportRun.Field.async_status]
            percent_complete = job[AdReportRun.Field.async_percent_completion]

            job_id = job["id"]
            self.logger.info(
                "%s for %s - %s. %s%% done. ",
                status,
                params["time_range"]["since"],
                params["time_range"]["until"],
                percent_complete,
            )

            if status == "Job Completed":
                return job
            if status == "Job Failed":
                raise RuntimeError(dict(job))
            if duration > INSIGHTS_MAX_WAIT_TO_START_SECONDS and percent_complete == 0:
                error_message = (
                    f"Insights job {job_id} did not start after "
                    f"{INSIGHTS_MAX_WAIT_TO_START_SECONDS} seconds. "
                    "This is an intermittent error and may resolve itself on subsequent "
                    "queries to the Facebook API. "
                    "You should deselect fields from the schema that are not necessary, "
                    "as that may help improve the reliability of the Facebook API."
                )
                raise RuntimeError(error_message)

            if duration > INSIGHTS_MAX_WAIT_TO_FINISH_SECONDS:
                error_message = (
                    f"Insights job {job_id} did not complete after "
                    f"{INSIGHTS_MAX_WAIT_TO_FINISH_SECONDS // 60} seconds. "
                    "This is an intermittent error and may resolve itself on "
                    "subsequent queries to the Facebook API. "
                    "You should deselect fields from the schema that are not necessary, "
                    "as that may help improve the reliability of the Facebook API."
                )
                raise RuntimeError(error_message)

            self.logger.info(
                "Sleeping for %s seconds until job is done",
                SLEEP_TIME_INCREMENT,
            )
            time.sleep(SLEEP_TIME_INCREMENT)
        msg = "Job failed to complete for unknown reason"
        raise RuntimeError(msg)

    def _get_start_date(
        self,
        context: dict | None,
    ) -> pendulum.Date:
        lookback_window = self.config["lookback_window"]

        config_start_date = pendulum.parse(self.config["start_date"]).date()
        incremental_start_date = pendulum.parse(
            self.get_starting_replication_key_value(context),
        ).date()

        lookback_start_date = incremental_start_date.subtract(days=lookback_window)

        # Don't use lookback if this is the first sync. Just start where the user requested.
        if config_start_date >= incremental_start_date:
            report_start = config_start_date
            self.logger.info("Using configured start_date as report start filter.")
        else:
            self.logger.info(
                "Incremental sync, applying lookback '%s' to the "
                "bookmark start_date '%s'. Syncing "
                "reports starting on '%s'.",
                lookback_window,
                incremental_start_date,
                lookback_start_date,
            )
            report_start = lookback_start_date

        # Facebook store metrics maximum of 37 months old. Any time range that
        # older that 37 months from current date would result in 400 Bad request
        # HTTP response.
        # https://developers.facebook.com/docs/marketing-api/reference/ad-account/insights/#overview
        today = pendulum.today().date()
        oldest_allowed_start_date = today.subtract(months=37)
        if report_start < oldest_allowed_start_date:
            report_start = oldest_allowed_start_date
            self.logger.info(
                "Report start date '%s' is older than 37 months. "
                "Using oldest allowed start date '%s' instead.",
                report_start,
                oldest_allowed_start_date,
            )
        return report_start

    def get_records(
        self,
        context: dict | None,
    ) -> t.Iterable[dict | tuple[dict, dict | None]]:
        self._initialize_client()

        time_increment = self._report_definition["time_increment_days"]

        sync_end_date = pendulum.parse(
            self.config.get("end_date", pendulum.today().to_date_string()),
        ).date()

        report_start = self._get_start_date(context)
        report_end = report_start.add(days=time_increment)

        while report_start <= sync_end_date:
            params = {
                "level": self._report_definition["level"],
                "fields": self._report_definition["fields"],
                "time_increment": time_increment,
                "breakdowns": self._report_definition["breakdowns"],
                "time_range": {
                    "since": report_start.to_date_string(),
                    "until": report_end.to_date_string(),
                },
            }
            job = self._run_job_to_completion(params)
            extracted_at = datetime.now(timezone.utc).isoformat()

            for obj in job.get_result():
                all_data = obj.export_all_data()
                # Add the extracted_at time to the record
                all_data["extracted_at"] = extracted_at
                yield all_data
            # Bump to the next increment
            report_start = report_start.add(days=time_increment)
            report_end = report_end.add(days=time_increment)
