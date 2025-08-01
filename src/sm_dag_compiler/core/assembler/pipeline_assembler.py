"""
Pipeline assembler that builds pipelines from a DAG structure and step builders.

This assembler leverages the specification-based dependency resolution system
to intelligently connect steps and build complete SageMaker pipelines.
"""

from typing import Dict, List, Any, Optional, Type, Set, Tuple
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.steps import Step
from sagemaker.workflow.parameters import ParameterString
from sagemaker.workflow.pipeline_context import PipelineSession
from pathlib import Path
import logging
import time
import traceback
from collections import defaultdict

from ..base import BasePipelineConfig, StepBuilderBase
from ..deps.registry_manager import RegistryManager
from ..deps.dependency_resolver import UnifiedDependencyResolver, create_dependency_resolver
from ..deps.factory import create_pipeline_components
from ..deps.property_reference import PropertyReference
from ..base import OutputSpec
from ...steps.registry.step_names import CONFIG_STEP_REGISTRY

from ...api.dag.base_dag import PipelineDAG


logger = logging.getLogger(__name__)


class PipelineAssembler:
    """
    Assembles pipeline steps using a DAG and step builders with specification-based dependency resolution.
    
    This class implements a component-based approach to building SageMaker Pipelines,
    leveraging the specification-based dependency resolution system to simplify
    the code and improve maintainability.
    
    The assembler follows these steps to build a pipeline:
    1. Initialize step builders for all steps in the DAG
    2. Determine the build order using topological sort
    3. Propagate messages between steps using the dependency resolver
    4. Instantiate steps in topological order, delegating input/output handling to builders
    5. Create the pipeline with the instantiated steps
    
    This approach allows for a flexible and modular pipeline definition, where
    each step is responsible for its own configuration and input/output handling.
    """
    # Dictionary to store Cradle data loading requests
    cradle_loading_requests = {}
    def __init__(
        self,
        dag: PipelineDAG,
        config_map: Dict[str, BasePipelineConfig],
        step_builder_map: Dict[str, Type[StepBuilderBase]],
        sagemaker_session: Optional[PipelineSession] = None,
        role: Optional[str] = None,
        pipeline_parameters: Optional[List[ParameterString]] = None,
        notebook_root: Optional[Path] = None,
        registry_manager: Optional[RegistryManager] = None,
        dependency_resolver: Optional[UnifiedDependencyResolver] = None
    ):
        """
        Initialize the pipeline assembler.
        
        Args:
            dag: PipelineDAG instance defining the pipeline structure
            config_map: Mapping from step name to config instance
            step_builder_map: Mapping from step type to StepBuilderBase subclass
            sagemaker_session: SageMaker session to use for creating the pipeline
            role: IAM role to use for the pipeline
            pipeline_parameters: List of pipeline parameters
            notebook_root: Root directory of the notebook environment
            registry_manager: Optional registry manager for dependency injection
            dependency_resolver: Optional dependency resolver for dependency injection
        """
        self.dag = dag
        self.config_map = config_map
        self.step_builder_map = step_builder_map
        self.sagemaker_session = sagemaker_session
        self.role = role
        self.notebook_root = notebook_root or Path.cwd()
        self.pipeline_parameters = pipeline_parameters or []
        
        # Store or create dependency components
        context_name = None
        for cfg in config_map.values():
            if hasattr(cfg, 'pipeline_name'):
                context_name = cfg.pipeline_name
                break
        
        # Use provided components or create new ones
        self._registry_manager = registry_manager or RegistryManager()
        registry = self._registry_manager.get_registry(context_name or "default")
        self._dependency_resolver = dependency_resolver or create_dependency_resolver(registry)

        self.step_instances: Dict[str, Step] = {}
        self.step_builders: Dict[str, StepBuilderBase] = {}
        
        # Store connections between steps
        self.step_messages = defaultdict(dict)
        
        # Validate inputs
        # Check that all nodes in the DAG have a corresponding config
        missing_configs = [node for node in self.dag.nodes if node not in self.config_map]
        if missing_configs:
            raise ValueError(f"Missing configs for nodes: {missing_configs}")
        
        # Check that all configs have a corresponding step builder
        for step_name, config in self.config_map.items():
            config_class_name = type(config).__name__
            # Use the centralized registry to get the canonical step type
            step_type = CONFIG_STEP_REGISTRY.get(config_class_name)
            if not step_type:
                # Fall back to old method if not in registry
                step_type = BasePipelineConfig.get_step_name(config_class_name)
                logger.warning(f"Config class {config_class_name} not found in registry, using derived name: {step_type}")
            
            if step_type not in self.step_builder_map:
                raise ValueError(f"Missing step builder for step type: {step_type}")
        
        # Check that all edges in the DAG connect nodes that exist in the DAG
        for src, dst in self.dag.edges:
            if src not in self.dag.nodes:
                raise ValueError(f"Edge source node not in DAG: {src}")
            if dst not in self.dag.nodes:
                raise ValueError(f"Edge destination node not in DAG: {dst}")
        
        logger.info("Input validation successful")
        
        # Initialize step builders
        self._initialize_step_builders()


    def _initialize_step_builders(self) -> None:
        """
        Initialize step builders for all steps in the DAG.
        
        This method creates a step builder instance for each step in the DAG,
        using the corresponding config from config_map and the appropriate
        builder class from step_builder_map.
        """
        logger.info("Initializing step builders")
        start_time = time.time()
        
        for step_name in self.dag.nodes:
            try:
                config = self.config_map[step_name]
                config_class_name = type(config).__name__
                # Use the centralized registry to get the canonical step type
                step_type = CONFIG_STEP_REGISTRY.get(config_class_name)
                if not step_type:
                    # Fall back to old method if not in registry
                    step_type = BasePipelineConfig.get_step_name(config_class_name)
                    logger.warning(f"Config class {config_class_name} not found in registry, using derived name: {step_type}")
                    
                builder_cls = self.step_builder_map[step_type]
                
                # Initialize the builder with dependency components
                builder = builder_cls(
                    config=config,
                    sagemaker_session=self.sagemaker_session,
                    role=self.role,
                    notebook_root=self.notebook_root,
                    registry_manager=self._registry_manager,       # Pass component
                    dependency_resolver=self._dependency_resolver,  # Pass component
                )
                self.step_builders[step_name] = builder
                logger.info(f"Initialized builder for step {step_name} of type {step_type}")
            except Exception as e:
                logger.error(f"Error initializing builder for step {step_name}: {e}")
                raise ValueError(f"Failed to initialize step builder for {step_name}: {e}") from e
        
        elapsed_time = time.time() - start_time
        logger.info(f"Initialized {len(self.step_builders)} step builders in {elapsed_time:.2f} seconds")


    def _propagate_messages(self) -> None:
        """
        Initialize step connections using the dependency resolver.
        
        This method analyzes the DAG structure and uses the dependency resolver
        to intelligently match inputs to outputs based on specifications.
        """
        logger.info("Initializing step connections using specifications")
        
        # Get dependency resolver
        resolver = self._get_dependency_resolver()
        
        # Process each edge in the DAG
        for src_step, dst_step in self.dag.edges:
            # Skip if builders don't exist
            if src_step not in self.step_builders or dst_step not in self.step_builders:
                continue
                
            # Get specs
            src_builder = self.step_builders[src_step]
            dst_builder = self.step_builders[dst_step]
            
            # Skip if no specifications
            if not hasattr(src_builder, 'spec') or not src_builder.spec or \
               not hasattr(dst_builder, 'spec') or not dst_builder.spec:
                continue
                
            # Let resolver match outputs to inputs
            for dep_name, dep_spec in dst_builder.spec.dependencies.items():
                matches = []
                
                # Check if source step can provide this dependency
                for out_name, out_spec in src_builder.spec.outputs.items():
                    compatibility = resolver._calculate_compatibility(dep_spec, out_spec, src_builder.spec)
                    if compatibility > 0.5:  # Same threshold as resolver
                        matches.append((out_name, out_spec, compatibility))
                
                # Use best match if found
                if matches:
                    # Sort by compatibility score
                    matches.sort(key=lambda x: x[2], reverse=True)
                    best_match = matches[0]
                    
                    # Check if there's already a better match
                    existing_match = self.step_messages.get(dst_step, {}).get(dep_name)
                    should_update = True

                    if existing_match:
                        existing_score = existing_match.get('compatibility', 0)
                        if existing_score >= best_match[2]:
                            should_update = False
                            logger.debug(f"Skipping lower-scoring match for {dst_step}.{dep_name}: {src_step}.{best_match[0]} (score: {best_match[2]:.2f} < existing: {existing_score:.2f})")

                    if should_update:
                        # Store in step_messages
                        self.step_messages[dst_step][dep_name] = {
                            'source_step': src_step,
                            'source_output': best_match[0],
                            'match_type': 'specification_match',
                            'compatibility': best_match[2]
                        }
                        logger.info(f"Matched {dst_step}.{dep_name} to {src_step}.{best_match[0]} (score: {best_match[2]:.2f})")

    def _generate_outputs(self, step_name: str) -> Dict[str, Any]:
        """
        Generate outputs dictionary using step builder's specification.
        
        This implementation leverages the step builder's specification
        to generate appropriate outputs.
        
        Args:
            step_name: Name of the step to generate outputs for
            
        Returns:
            Dictionary with output paths based on specification
        """
        builder = self.step_builders[step_name]
        config = self.config_map[step_name]
        
        # If builder has no specification, return empty dict
        if not hasattr(builder, 'spec') or not builder.spec:
            logger.warning(f"Step {step_name} has no specification, returning empty outputs")
            return {}
        
        # Get base S3 location - single source of truth
        base_s3_loc = getattr(config, 'pipeline_s3_loc', 's3://default-bucket/pipeline')
        
        # Generate outputs dictionary based on specification
        outputs = {}
        step_type = builder.spec.step_type.lower()
        
        # Use each output specification to generate standard output path
        for logical_name, output_spec in builder.spec.outputs.items():
            # Standard path pattern: {base_s3_loc}/{step_type}/{logical_name}
            outputs[logical_name] = f"{base_s3_loc}/{step_type}/{logical_name}"
            
            # Add debug log
            logger.debug(f"Generated output for {step_name}.{logical_name}: {outputs[logical_name]}")
            
        return outputs
    


    def _instantiate_step(self, step_name: str) -> Step:
        """
        Instantiate a pipeline step with appropriate inputs from dependencies.
        
        This method creates a step using the step builder's create_step method,
        delegating input extraction and output generation to the builder.
        
        Args:
            step_name: Name of the step to instantiate
            
        Returns:
            Instantiated SageMaker Pipeline Step
        """
        builder = self.step_builders[step_name]
        
        # Get dependency steps
        dependencies = []
        for dep_name in self.dag.get_dependencies(step_name):
            if dep_name in self.step_instances:
                dependencies.append(self.step_instances[dep_name])
        
        # Extract parameters from message dictionaries for backward compatibility
        inputs = {}
        if step_name in self.step_messages:
            for input_name, message in self.step_messages[step_name].items():
                src_step = message['source_step']
                src_output = message['source_output']
                if src_step in self.step_instances:
                    # Try to get the source step's builder to access its specifications
                    src_builder = self.step_builders.get(src_step)
                    output_spec = None
                    
                    # Try to find the output spec for this output name
                    if src_builder and hasattr(src_builder, 'spec') and src_builder.spec:
                        output_spec = src_builder.spec.get_output_by_name_or_alias(src_output)
                    
                    if output_spec:
                        try:
                            # Create a PropertyReference object
                            prop_ref = PropertyReference(
                                step_name=src_step,
                                output_spec=output_spec
                            )
                            
                            # Use the enhanced to_runtime_property method to get an actual SageMaker Properties object
                            runtime_prop = prop_ref.to_runtime_property(self.step_instances)
                            inputs[input_name] = runtime_prop
                            
                            logger.debug(f"Created runtime property reference for {step_name}.{input_name} -> {src_step}.{output_spec.property_path}")
                        except Exception as e:
                            # Log the error and fall back to a safe string
                            logger.warning(f"Error creating runtime property reference: {str(e)}")
                            s3_uri = f"s3://pipeline-reference/{src_step}/{src_output}"
                            inputs[input_name] = s3_uri
                            logger.warning(f"Using S3 URI fallback: {s3_uri}")
                    else:
                        # Create a safe string reference as a fallback
                        s3_uri = f"s3://pipeline-reference/{src_step}/{src_output}"
                        inputs[input_name] = s3_uri
                        logger.warning(f"Could not find output spec for {src_step}.{src_output}, using S3 URI placeholder: {s3_uri}")
        
        # Generate outputs using the specification
        outputs = self._generate_outputs(step_name)
        
        # Create step with extracted inputs and outputs
        kwargs = {
            'inputs': inputs,
            'outputs': outputs,
            'dependencies': dependencies,
            'enable_caching': True
        }
        
        try:
            step = builder.create_step(**kwargs)
            logger.info(f"Built step {step_name}")
            
            # Special case for CradleDataLoading steps - store request dict for execution document
            config = self.config_map[step_name]
            step_type = BasePipelineConfig.get_step_name(type(config).__name__)
            if step_type == "CradleDataLoading" and hasattr(builder, "get_request_dict"):
                self.cradle_loading_requests[step.name] = builder.get_request_dict()
                logger.info(f"Stored Cradle data loading request for step: {step.name}")
            
            return step
        except Exception as e:
            logger.error(f"Error building step {step_name}: {e}")
            raise ValueError(f"Failed to build step {step_name}: {e}") from e


    @classmethod
    def create_with_components(cls, 
                             dag: PipelineDAG,
                             config_map: Dict[str, BasePipelineConfig],
                             step_builder_map: Dict[str, Type[StepBuilderBase]],
                             context_name: Optional[str] = None,
                             **kwargs) -> "PipelineAssembler":
        """
        Create pipeline assembler with managed components.
        
        This factory method creates a pipeline assembler with properly configured
        dependency components from the factory module.
        
        Args:
            dag: PipelineDAG instance defining the pipeline structure
            config_map: Mapping from step name to config instance
            step_builder_map: Mapping from step type to StepBuilderBase subclass
            context_name: Optional context name for registry
            **kwargs: Additional arguments to pass to the constructor
            
        Returns:
            Configured PipelineAssembler instance
        """
        components = create_pipeline_components(context_name)
        return cls(
            dag=dag,
            config_map=config_map,
            step_builder_map=step_builder_map,
            registry_manager=components["registry_manager"],
            dependency_resolver=components["resolver"],
            **kwargs
        )
    
    def _get_registry_manager(self) -> RegistryManager:
        """Get the registry manager."""
        return self._registry_manager
    
    def _get_dependency_resolver(self) -> UnifiedDependencyResolver:
        """Get the dependency resolver."""
        return self._dependency_resolver
    
    def generate_pipeline(self, pipeline_name: str) -> Pipeline:
        """
        Build and return a SageMaker Pipeline object.
        
        This method builds the pipeline by:
        1. Propagating messages between steps using specification-based matching
        2. Instantiating steps in topological order
        3. Creating the pipeline with the instantiated steps
        
        Args:
            pipeline_name: Name of the pipeline
            
        Returns:
            SageMaker Pipeline object
        """
        logger.info(f"Generating pipeline: {pipeline_name}")
        start_time = time.time()
        
        # Reset step instances if we're regenerating the pipeline
        if self.step_instances:
            logger.info("Clearing existing step instances for pipeline regeneration")
            self.step_instances = {}
        
        # Propagate messages between steps
        self._propagate_messages()
        
        # Topological sort to determine build order
        try:
            build_order = self.dag.topological_sort()
            logger.info(f"Build order: {build_order}")
        except ValueError as e:
            logger.error(f"Error in topological sort: {e}")
            raise ValueError(f"Failed to determine build order: {e}") from e
        
        # Instantiate steps in topological order
        for step_name in build_order:
            try:
                step = self._instantiate_step(step_name)
                self.step_instances[step_name] = step
            except Exception as e:
                logger.error(f"Error instantiating step {step_name}: {e}")
                raise ValueError(f"Failed to instantiate step {step_name}: {e}") from e

        # Create the pipeline
        steps = [self.step_instances[name] for name in build_order]
        pipeline = Pipeline(
            name=pipeline_name,
            parameters=self.pipeline_parameters,
            steps=steps,
            sagemaker_session=self.sagemaker_session,
        )
        
        elapsed_time = time.time() - start_time
        logger.info(f"Generated pipeline {pipeline_name} with {len(steps)} steps in {elapsed_time:.2f} seconds")
        
        return pipeline
