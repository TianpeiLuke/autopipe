from typing import Dict, Optional, Any, List, Set, Union
from pathlib import Path
import logging
import os
import json
import importlib
from datetime import datetime

from sagemaker.workflow.steps import ProcessingStep, Step
from sagemaker.workflow.steps import CacheConfig
from ...core.deps.registry_manager import RegistryManager
from ...core.deps.dependency_resolver import UnifiedDependencyResolver

# Import CradleDataLoadingStep
from secure_ai_sandbox_workflow_python_sdk.cradle_data_loading.cradle_data_loading_step import (
    CradleDataLoadingStep,
)

# Import Cradle models for request building
try:
    from com.amazon.secureaisandboxproxyservice.models.field import Field
    from com.amazon.secureaisandboxproxyservice.models.datasource import DataSource
    from com.amazon.secureaisandboxproxyservice.models.mdsdatasourceproperties import MdsDataSourceProperties
    from com.amazon.secureaisandboxproxyservice.models.edxdatasourceproperties import EdxDataSourceProperties
    from com.amazon.secureaisandboxproxyservice.models.andesdatasourceproperties import AndesDataSourceProperties
    from com.amazon.secureaisandboxproxyservice.models.datasourcesspecification import DataSourcesSpecification
    from com.amazon.secureaisandboxproxyservice.models.jobsplitoptions import JobSplitOptions
    from com.amazon.secureaisandboxproxyservice.models.transformspecification import TransformSpecification
    from com.amazon.secureaisandboxproxyservice.models.outputspecification import OutputSpecification
    from com.amazon.secureaisandboxproxyservice.models.cradlejobspecification import CradleJobSpecification
    from com.amazon.secureaisandboxproxyservice.models.createcradledataloadjobrequest import CreateCradleDataLoadJobRequest
    CRADLE_MODELS_AVAILABLE = True
except ImportError:
    logger = logging.getLogger(__name__)
    logger.warning("Cradle models not available. _build_request and get_request_dict will not work.")
    CRADLE_MODELS_AVAILABLE = False

# Import coral utils for request conversion
try:
    from secure_ai_sandbox_python_lib.utils import coral_utils
    CORAL_UTILS_AVAILABLE = True
except ImportError:
    logger = logging.getLogger(__name__)
    logger.warning("coral_utils not available. get_request_dict will not work.")
    CORAL_UTILS_AVAILABLE = False

# Import the script contract
try:
    from ..contracts.cradle_data_loading_contract import CRADLE_DATA_LOADING_CONTRACT
    CONTRACT_AVAILABLE = True
except ImportError:
    logger = logging.getLogger(__name__)
    logger.warning("Cradle data loading contract not available. Contract-driven path resolution will not work.")
    CRADLE_DATA_LOADING_CONTRACT = None
    CONTRACT_AVAILABLE = False

from ..configs.config_data_load_step_cradle import CradleDataLoadConfig
from ...core.base.builder_base import StepBuilderBase
from ..registry.builder_registry import register_builder

# Import constants from the same module used by the data loading step
try:
    from secure_ai_sandbox_workflow_python_sdk.utils.constants import (
        OUTPUT_TYPE_DATA,
        OUTPUT_TYPE_METADATA,
        OUTPUT_TYPE_SIGNATURE,
    )
except ImportError:
     # Fallback to string constants if import fails
    OUTPUT_TYPE_DATA = "DATA"  # Upper Case, correct one
    OUTPUT_TYPE_METADATA = "METADATA"  # Upper Case, correct one
    OUTPUT_TYPE_SIGNATURE = "SIGNATURE"  # Upper Case, correct one

logger = logging.getLogger(__name__)


@register_builder()
class CradleDataLoadingStepBuilder(StepBuilderBase):
    """
    Builder for a Cradle Data Loading ProcessingStep.
    This class is responsible for configuring and creating a SageMaker ProcessingStep
    that executes the Cradle data loading script.
    """

    def __init__(
        self,
        config: CradleDataLoadConfig,
        sagemaker_session=None,
        role: Optional[str] = None,
        notebook_root: Optional[Path] = None,
        registry_manager: Optional["RegistryManager"] = None,
        dependency_resolver: Optional["UnifiedDependencyResolver"] = None
    ):
        """
        Initializes the builder with a specific configuration for the data loading step.

        Args:
            config: A CradleDataLoadConfig instance containing all necessary settings.
            sagemaker_session: The SageMaker session object to manage interactions with AWS.
            role: The IAM role ARN to be used by the SageMaker Processing Job.
            notebook_root: The root directory of the notebook environment, used for resolving
                         local paths if necessary.
            registry_manager: Optional registry manager for dependency injection
            dependency_resolver: Optional dependency resolver for dependency injection
        """
        if not isinstance(config, CradleDataLoadConfig):
            raise ValueError(
                "CradleDataLoadingStepBuilder requires a CradleDataLoadConfig instance."
            )
            
        # Select specification based on job type
        spec = None
        if hasattr(config, 'job_type'):
            job_type = config.job_type.lower()
            if job_type == "training":
                from ..specs.data_loading_training_spec import DATA_LOADING_TRAINING_SPEC
                spec = DATA_LOADING_TRAINING_SPEC
                self.log_info("Using training-specific DATA_LOADING_TRAINING_SPEC")
            elif job_type == "validation":
                from ..specs.data_loading_validation_spec import DATA_LOADING_VALIDATION_SPEC
                spec = DATA_LOADING_VALIDATION_SPEC
                self.log_info("Using validation-specific DATA_LOADING_VALIDATION_SPEC")
            elif job_type == "testing":
                from ..specs.data_loading_testing_spec import DATA_LOADING_TESTING_SPEC
                spec = DATA_LOADING_TESTING_SPEC
                self.log_info("Using testing-specific DATA_LOADING_TESTING_SPEC")
            elif job_type == "calibration":
                from ..specs.data_loading_calibration_spec import DATA_LOADING_CALIBRATION_SPEC
                spec = DATA_LOADING_CALIBRATION_SPEC
                self.log_info("Using calibration-specific DATA_LOADING_CALIBRATION_SPEC")
            
            # If no specific type-based spec is found, try to use the generic one
            if spec is None:
                try:
                    from ..specs.data_loading_spec import DATA_LOADING_SPEC
                    spec = DATA_LOADING_SPEC
                    self.log_info("Using generic DATA_LOADING_SPEC for job type: %s", job_type)
                except ImportError:
                    self.log_warning("No specification found for job type: %s", job_type)
                
        super().__init__(
            config=config,
            spec=spec,
            sagemaker_session=sagemaker_session,
            role=role,
            notebook_root=notebook_root,
            registry_manager=registry_manager,
            dependency_resolver=dependency_resolver
        )
        self.config: CradleDataLoadConfig = config
        
        # Store contract reference
        self.contract = CRADLE_DATA_LOADING_CONTRACT if CONTRACT_AVAILABLE else None
        
        if self.spec and not self.contract:
            self.log_warning("Script contract not available - path resolution will use hardcoded values")

    def validate_configuration(self) -> None:
        """
        Called by StepBuilderBase.__init__(). Ensures required fields are set
        and in the correct format.

        In particular:
          - job_type ∈ {'training','validation','testing','calibration'}
          - At least one data source in data_sources_spec
          - Each MDS/EDX/ANDES config is present if indicated
          - start_date and end_date must exactly match 'YYYY-mm-DDTHH:MM:SS'
          - start_date < end_date
        """
        self.log_info("Validating CradleDataLoadConfig…")

        # (1) job_type is already validated by Pydantic, but double-check presence:
        valid_job_types = {'training', 'validation', 'testing', 'calibration'}
        if not self.config.job_type:
            raise ValueError("job_type must be provided (e.g. 'training','validation','testing','calibration').")
        if self.config.job_type.lower() not in valid_job_types:
            raise ValueError(f"job_type must be one of: {valid_job_types}")


        # (2) data_sources_spec must have at least one entry
        ds_list = self.config.data_sources_spec.data_sources
        if not ds_list or len(ds_list) == 0:
            raise ValueError("At least one DataSourceConfig must be provided in data_sources_spec.data_sources")

        # (3) For each data_source, check that required subfields are present
        for idx, ds_cfg in enumerate(ds_list):
            if ds_cfg.data_source_type == "MDS":
                mds_props = ds_cfg.mds_data_source_properties
                if mds_props is None:
                    raise ValueError(f"DataSource #{idx} is MDS but mds_data_source_properties was not provided.")
                # MdsDataSourceConfig validators have already run.
            elif ds_cfg.data_source_type == "EDX":
                edx_props = ds_cfg.edx_data_source_properties
                if edx_props is None:
                    raise ValueError(f"DataSource #{idx} is EDX but edx_data_source_properties was not provided.")
                # Check EDX manifest
                if not edx_props.edx_manifest:
                    raise ValueError(f"DataSource #{idx} EDX manifest must be a nonempty string.")
            elif ds_cfg.data_source_type == "ANDES":
                andes_props = ds_cfg.andes_data_source_properties
                if andes_props is None:
                    raise ValueError(f"DataSource #{idx} is ANDES but andes_data_source_properties was not provided.")
            else:
                raise ValueError(f"DataSource #{idx} has invalid type: {ds_cfg.data_source_type}")

        # (4) Check that start_date & end_date match exact format YYYY-mm-DDTHH:MM:SS
        for field_name in ("start_date", "end_date"):
            value = getattr(self.config.data_sources_spec, field_name)
            try:
                parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
            except Exception:
                raise ValueError(
                    f"'{field_name}' must be in format YYYY-mm-DD'T'HH:MM:SS "
                    f"(e.g. '2025-01-01T00:00:00'), got: {value!r}"
                )
            if parsed.strftime("%Y-%m-%dT%H:%M:%S") != value:
                raise ValueError(
                    f"'{field_name}' does not match the required format exactly; got {value!r}"
                )

        # (5) Also ensure start_date < end_date
        s = datetime.strptime(self.config.data_sources_spec.start_date, "%Y-%m-%dT%H:%M:%S")
        e = datetime.strptime(self.config.data_sources_spec.end_date, "%Y-%m-%dT%H:%M:%S")
        if s >= e:
            raise ValueError("start_date must be strictly before end_date.")

        # (6) Everything else (output_path S3 URI, output_format, cluster_type, etc.) 
        #     is validated by Pydantic already.
        
        # (7) Validate spec-contract alignment if both are available
        if self.spec and self.contract:
            # Check if output logical names in spec match expected output paths in contract
            for output in self.spec.outputs:
                logical_name = output.logical_name
                if logical_name not in self.contract.expected_output_paths:
                    logger.warning(f"Output '{logical_name}' in spec not found in contract expected_output_paths")

        self.log_info("CradleDataLoadConfig validation succeeded.")
        
    def _get_inputs(self, inputs: Dict[str, Any]) -> List[Any]:
        """
        Get inputs for the step.
        
        CradleDataLoadingStep typically doesn't have inputs from previous pipeline steps,
        as it's usually the first step that loads data from external sources.
        
        Args:
            inputs: Dictionary mapping logical names to input sources
            
        Returns:
            Empty list as CradleDataLoading doesn't take processing inputs
        """
        # CradleDataLoading doesn't typically have inputs from prior steps
        return []
        
    def _get_outputs(self, outputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get outputs for the step.
        
        CradleDataLoadingStep uses a different output mechanism than standard ProcessingStep,
        so this method returns an empty dictionary. However, it logs information from the
        contract about expected output paths if available.
        
        Args:
            outputs: Dictionary mapping logical names to output destinations
            
        Returns:
            Empty dictionary as CradleDataLoading handles outputs differently
        """
        # CradleDataLoading uses a different output mechanism
        # But we can use the contract to validate and log output paths
        if self.contract and hasattr(self.contract, 'expected_output_paths'):
            for logical_name, container_path in self.contract.expected_output_paths.items():
                self.log_info("Contract defines output path for '%s': %s", logical_name, container_path)
                
        # The actual outputs are defined in the CradleDataLoadingStep itself
        return {}
        
    def create_step(self, **kwargs) -> Step:
        """
        Creates a specialized CradleDataLoadingStep for Cradle data loading.
        
        This method creates a CradleDataLoadingStep that directly interacts with the
        Cradle service to load data. The step is a custom implementation that inherits
        from MODSPredefinedProcessingStep rather than a standard SageMaker ProcessingStep.

        Args:
            **kwargs: Keyword arguments for configuring the step, including:
                - dependencies: Optional list of steps that this step depends on.
                - enable_caching: A boolean indicating whether to cache the results of this step.

        Returns:
            Step: A CradleDataLoadingStep instance with added output attributes.
            
        Raises:
            ValueError: If there's an error creating the CradleDataLoadingStep.
        """
            
        # Create the step name using standardized step name generation
        step_name = self._get_step_name()  # Already includes job_type if present
        
        self.log_info("Creating CradleDataLoadingStep...")
        try:
            # Extract dependencies from kwargs
            dependencies = kwargs.get('dependencies', [])
            enable_caching = kwargs.get('enable_caching', True)
            
            # Create a CradleDataLoadingStep - this is a custom step that handles its own
            # initialization differently than standard SageMaker ProcessingStep
            step = CradleDataLoadingStep(
                step_name=step_name,
                role=self.role,
                sagemaker_session=self.session
            )
            
            # Add dependencies if provided
            if dependencies:
                step.add_depends_on(dependencies)
                
            # Handle caching (if CradleDataLoadingStep supports it)
            if not enable_caching and hasattr(step, 'cache_config'):
                step.cache_config.enable_caching = False
            
            # Store specification and contract in the step for future reference
            # This enables the specification-driven approach to work with the step
            if hasattr(self, 'spec') and self.spec:
                setattr(step, '_spec', self.spec)
            if self.contract:
                setattr(step, '_contract', self.contract)
            
            self.log_info("Created CradleDataLoadingStep with name: %s", step.name)
            
            # Get the output locations for logging
            output_locations = step.get_output_locations()
            self.log_info("CradleDataLoadingStep output locations: %s", output_locations)
            
            return step
            
        except Exception as e:
            self.log_error("Error creating CradleDataLoadingStep: %s", e)
            raise ValueError(f"Failed to create CradleDataLoadingStep: {e}") from e
        
    def _build_request(self) -> Any:
        """
        Convert self.config → a CreateCradleDataLoadJobRequest instance under the hood.
        
        This method builds a Cradle data load request from the configuration, which can be
        used to fill in the execution document or for logging purposes.
        
        Returns:
            CreateCradleDataLoadJobRequest: The request object for Cradle data loading
            
        Raises:
            ImportError: If the required Cradle models are not available
        """
        if not CRADLE_MODELS_AVAILABLE:
            raise ImportError("Cradle models not available. Cannot build request.")
            
        # Check if we have the necessary configuration attributes
        required_attrs = [
            'data_sources_spec',
            'transform_spec',
            'output_spec',
            'cradle_job_spec'
        ]
        
        for attr in required_attrs:
            if not hasattr(self.config, attr) or getattr(self.config, attr) is None:
                raise ValueError(f"CradleDataLoadConfig missing required attribute: {attr}")
        
        try:
            # (a) Build each DataSource from data_sources_spec.data_sources
            data_source_models: List[DataSource] = []
            for ds_cfg in self.config.data_sources_spec.data_sources:
                if ds_cfg.data_source_type == "MDS":
                    mds_props_cfg = ds_cfg.mds_data_source_properties
                    mds_props = MdsDataSourceProperties(
                        service_name=mds_props_cfg.service_name,
                        org_id=mds_props_cfg.org_id,
                        region=mds_props_cfg.region,
                        output_schema=[
                            Field(field_name=f["field_name"], field_type=f["field_type"])
                            for f in mds_props_cfg.output_schema
                        ],
                        use_hourly_edx_data_set=mds_props_cfg.use_hourly_edx_data_set,
                    )
                    data_source_models.append(
                        DataSource(
                            data_source_name=ds_cfg.data_source_name,
                            data_source_type="MDS",
                            mds_data_source_properties=mds_props,
                            edx_data_source_properties=None,
                        )
                    )

                elif ds_cfg.data_source_type == "EDX":
                    edx_props_cfg = ds_cfg.edx_data_source_properties
                    edx_props = EdxDataSourceProperties(
                        edx_arn=edx_props_cfg.edx_manifest,
                        schema_overrides=[
                            Field(field_name=f["field_name"], field_type=f["field_type"])
                            for f in edx_props_cfg.schema_overrides
                        ],
                    )
                    data_source_models.append(
                        DataSource(
                            data_source_name=ds_cfg.data_source_name,
                            data_source_type="EDX",
                            mds_data_source_properties=None,
                            edx_data_source_properties=edx_props,
                        )
                    )
                elif ds_cfg.data_source_type == "ANDES":
                    andes_props_cfg = ds_cfg.andes_data_source_properties
                    if andes_props_cfg.andes3_enabled:
                        self.log_info("ANDES 3.0 is enabled for table %s", andes_props_cfg.table_name)
                    andes_props = AndesDataSourceProperties(
                        provider=andes_props_cfg.provider,
                        table_name=andes_props_cfg.table_name,
                        andes3_enabled=andes_props_cfg.andes3_enabled,
                    )
                    data_source_models.append(
                        DataSource(
                            data_source_name=ds_cfg.data_source_name,
                            data_source_type="ANDES",
                            mds_data_source_properties=None,
                            edx_data_source_properties=None,
                            andes_data_source_properties=andes_props,
                        )
                    )

            # (b) DataSourcesSpecification
            ds_spec_cfg = self.config.data_sources_spec
            data_sources_spec = DataSourcesSpecification(
                start_date=ds_spec_cfg.start_date,
                end_date=ds_spec_cfg.end_date,
                data_sources=data_source_models,
            )

            # (c) TransformSpecification
            transform_spec_cfg = self.config.transform_spec
            jso = transform_spec_cfg.job_split_options
            split_opts = JobSplitOptions(
                split_job=jso.split_job,
                days_per_split=jso.days_per_split,
                merge_sql=jso.merge_sql or "",
            )
            transform_spec = TransformSpecification(
                transform_sql=transform_spec_cfg.transform_sql,
                job_split_options=split_opts,
            )

            # (d) OutputSpecification
            output_spec_cfg = self.config.output_spec
            output_spec = OutputSpecification(
                output_schema=output_spec_cfg.output_schema,
                output_path=output_spec_cfg.output_path,
                output_format=output_spec_cfg.output_format,
                output_save_mode=output_spec_cfg.output_save_mode,
                output_file_count=output_spec_cfg.output_file_count,
                keep_dot_in_output_schema=output_spec_cfg.keep_dot_in_output_schema,
                include_header_in_s3_output=output_spec_cfg.include_header_in_s3_output,
            )

            # (e) CradleJobSpecification
            cradle_job_spec_cfg = self.config.cradle_job_spec
            cradle_job_spec = CradleJobSpecification(
                cluster_type=cradle_job_spec_cfg.cluster_type,
                cradle_account=cradle_job_spec_cfg.cradle_account,
                extra_spark_job_arguments=cradle_job_spec_cfg.extra_spark_job_arguments or "",
                job_retry_count=cradle_job_spec_cfg.job_retry_count,
            )

            # (f) Build the final CreateCradleDataLoadJobRequest
            request = CreateCradleDataLoadJobRequest(
                data_sources=data_sources_spec,
                transform_specification=transform_spec,
                output_specification=output_spec,
                cradle_job_specification=cradle_job_spec,
            )

            return request
            
        except Exception as e:
            self.log_error("Error building Cradle request: %s", e)
            raise ValueError(f"Failed to build Cradle request: {e}") from e
    
    def get_request_dict(self) -> Dict[str, Any]:
        """
        Return the CradleDataLoad request as a plain Python dict.
        
        This method is useful for logging or for passing to StepOperator.
        It builds the request using _build_request and then converts it to a dictionary.
        
        Returns:
            Dict[str, Any]: The request as a dictionary
            
        Raises:
            ImportError: If coral_utils is not available
            ValueError: If the request could not be built
        """
        if not CORAL_UTILS_AVAILABLE:
            raise ImportError("coral_utils not available. Cannot convert request to dict.")
            
        try:
            request = self._build_request()
            return coral_utils.convert_coral_to_dict(request)
        except Exception as e:
            self.log_error("Error getting request dict: %s", e)
            raise ValueError(f"Failed to get request dict: {e}") from e
            
    def get_output_location(self, step: CradleDataLoadingStep, output_type: str) -> str:
        """
        Get a specific output location from a created CradleDataLoadingStep.
        
        This method uses the specification-driven approach to get output locations,
        falling back to the step's get_output_locations method if needed.
        
        Args:
            step (CradleDataLoadingStep): The CradleDataLoadingStep created by this builder
            output_type (str): The output type to retrieve. Valid values are:
                             - OUTPUT_TYPE_DATA: Main data output location
                             - OUTPUT_TYPE_METADATA: Metadata output location
                             - OUTPUT_TYPE_SIGNATURE: Signature output location
                             
        Returns:
            str: S3 location for the specified output type
            
        Raises:
            ValueError: If the step is not a CradleDataLoadingStep instance or if
                      the requested output_type is not valid
        """
        if not isinstance(step, CradleDataLoadingStep):
            raise ValueError("Argument must be a CradleDataLoadingStep instance")
            
        # Map output type to logical name
        output_type_to_logical_name = {
            OUTPUT_TYPE_DATA: "DATA",
            OUTPUT_TYPE_METADATA: "METADATA", 
            OUTPUT_TYPE_SIGNATURE: "SIGNATURE"
        }
        
        logical_name = output_type_to_logical_name.get(output_type)
        if not logical_name:
            raise ValueError(f"Invalid output_type: {output_type}. Valid values are: {list(output_type_to_logical_name.keys())}")
            
        # Use specification-based property path if available
        if self.spec and hasattr(step, '_spec'):
            for output in self.spec.outputs:
                if output.logical_name == logical_name:
                    property_path = output.property_path
                    if property_path:
                        # Use dynamic property access to get the value from the step
                        try:
                            path_parts = property_path.split('.')
                            value = step
                            for part in path_parts:
                                # Handle array indexing with string key
                                if '[' in part and ']' in part:
                                    base_part = part.split('[')[0]
                                    key = part.split('[')[1].split(']')[0].strip("'\"")
                                    value = getattr(value, base_part)[key]
                                else:
                                    value = getattr(value, part)
                            return value
                        except Exception as e:
                            self.log_warning("Error accessing property path %s: %s", property_path, e)
        
        # Fall back to the step's built-in method
        return step.get_output_locations(output_type)
        
    def get_step_outputs(self, step: CradleDataLoadingStep, output_type: str = None) -> Union[Dict[str, str], str]:
        """
        Get the output locations from a created CradleDataLoadingStep.
        
        This method is a higher-level interface to get_output_location that can return
        either all outputs or a specific output.
        
        Args:
            step (CradleDataLoadingStep): The CradleDataLoadingStep created by this builder
            output_type (str, optional): Specific output type to retrieve. If None, returns all output types.
                                       Valid values are OUTPUT_TYPE_DATA, OUTPUT_TYPE_METADATA, OUTPUT_TYPE_SIGNATURE.
                                       
        Returns:
            Union[Dict[str, str], str]: 
                - If output_type is None: Dictionary mapping output types to their S3 locations
                - If output_type is specified: S3 location for the specified output type
                
        Raises:
            ValueError: If the step is not a CradleDataLoadingStep instance or if
                      the requested output_type is not valid
        """
        if output_type:
            return self.get_output_location(step, output_type)
            
        # Get all outputs - use the step's built-in method for simplicity
        return step.get_output_locations()
