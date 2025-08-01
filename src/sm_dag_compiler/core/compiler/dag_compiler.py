"""
DAG Compiler for the Pipeline API.

This module provides the main API functions for compiling PipelineDAG structures
into executable SageMaker pipelines.
"""

from typing import Optional, Dict, Any, Tuple
import logging
from pathlib import Path

from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.pipeline_context import PipelineSession

from ...api.dag.base_dag import PipelineDAG
from .dynamic_template import DynamicPipelineTemplate
from .config_resolver import StepConfigResolver
from ...steps.registry.builder_registry import StepBuilderRegistry
from .validation import ValidationResult, ResolutionPreview, ConversionReport, ValidationEngine
from .exceptions import PipelineAPIError, ConfigurationError, ValidationError
from ...steps.registry.exceptions import RegistryError

logger = logging.getLogger(__name__)


def compile_dag_to_pipeline(
    dag: PipelineDAG,
    config_path: str,
    sagemaker_session: Optional[PipelineSession] = None,
    role: Optional[str] = None,
    pipeline_name: Optional[str] = None,
    **kwargs
) -> Pipeline:
    """
    Compile a PipelineDAG into a complete SageMaker Pipeline.
    
    This is the main entry point for users who want a simple, one-call
    compilation from DAG to pipeline.
    
    Args:
        dag: PipelineDAG instance defining the pipeline structure
        config_path: Path to configuration file containing step configs
        sagemaker_session: SageMaker session for pipeline execution
        role: IAM role for pipeline execution
        pipeline_name: Optional pipeline name override
        **kwargs: Additional arguments passed to template constructor
        
    Returns:
        Generated SageMaker Pipeline ready for execution
        
    Raises:
        ValueError: If DAG nodes don't have corresponding configurations
        ConfigurationError: If configuration validation fails
        RegistryError: If step builders not found for config types
        
    Example:
        >>> dag = PipelineDAG()
        >>> dag.add_node("data_load")
        >>> dag.add_node("preprocess")
        >>> dag.add_edge("data_load", "preprocess")
        >>> 
        >>> pipeline = compile_dag_to_pipeline(
        ...     dag=dag,
        ...     config_path="configs/my_pipeline.json",
        ...     sagemaker_session=session,
        ...     role="arn:aws:iam::123456789012:role/SageMakerRole"
        ... )
        >>> pipeline.upsert()
    """
    try:
        logger.info(f"Compiling DAG with {len(dag.nodes)} nodes to pipeline")
        
        # Validate inputs
        if not isinstance(dag, PipelineDAG):
            raise ValueError("dag must be a PipelineDAG instance")
        
        if not dag.nodes:
            raise ValueError("DAG must contain at least one node")
        
        config_path_obj = Path(config_path)
        if not config_path_obj.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        
        # Create compiler
        compiler = PipelineDAGCompiler(
            config_path=config_path,
            sagemaker_session=sagemaker_session,
            role=role,
            **kwargs
        )
        
        # Use compile method which uses our create_template method
        pipeline = compiler.compile(dag, pipeline_name=pipeline_name)
        
        logger.info(f"Successfully compiled DAG to pipeline: {pipeline.name}")
        return pipeline
        
    except Exception as e:
        logger.error(f"Failed to compile DAG to pipeline: {e}")
        raise PipelineAPIError(f"DAG compilation failed: {e}") from e


class PipelineDAGCompiler:
    """
    Advanced API for DAG-to-template compilation with additional control.
    
    This class provides more control over the compilation process, including
    validation, debugging, and customization options.
    """
    
    def __init__(
        self,
        config_path: str,
        sagemaker_session: Optional[PipelineSession] = None,
        role: Optional[str] = None,
        config_resolver: Optional[StepConfigResolver] = None,
        builder_registry: Optional[StepBuilderRegistry] = None,
        **kwargs
    ):
        """
        Initialize compiler with configuration and session.
        
        Args:
            config_path: Path to configuration file
            sagemaker_session: SageMaker session for pipeline execution
            role: IAM role for pipeline execution
            config_resolver: Custom config resolver (optional)
            builder_registry: Custom builder registry (optional)
            **kwargs: Additional arguments for template constructor
        """
        self.config_path = config_path
        self.sagemaker_session = sagemaker_session
        self.role = role
        self.template_kwargs = kwargs
        
        # Initialize components
        self.config_resolver = config_resolver or StepConfigResolver()
        self.builder_registry = builder_registry or StepBuilderRegistry()
        self.validation_engine = ValidationEngine()
        
        self.logger = logging.getLogger(__name__)
        
        # Validate config file exists
        config_path_obj = Path(config_path)
        if not config_path_obj.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
    
    def validate_dag_compatibility(self, dag: PipelineDAG) -> ValidationResult:
        """
        Validate that DAG nodes have corresponding configurations.
        
        Returns detailed validation results including:
        - Missing configurations
        - Unresolvable step builders
        - Configuration validation errors
        - Dependency resolution issues
        
        Args:
            dag: PipelineDAG instance to validate
            
        Returns:
            ValidationResult with detailed validation information
        """
        try:
            self.logger.info(f"Validating DAG compatibility for {len(dag.nodes)} nodes")
            
            # Create a template using our create_template method
            temp_template = self.create_template(dag)
            
            # Get resolved mappings
            dag_nodes = list(dag.nodes)
            available_configs = temp_template.configs
            
            try:
                config_map = temp_template._create_config_map()
            except Exception as e:
                # If config resolution fails, create partial validation result
                return ValidationResult(
                    is_valid=False,
                    missing_configs=dag_nodes,
                    unresolvable_builders=[],
                    config_errors={'resolution': [str(e)]},
                    dependency_issues=[],
                    warnings=[]
                )
            
            try:
                builder_map = temp_template._create_step_builder_map()
            except Exception as e:
                # If builder resolution fails, create partial validation result
                return ValidationResult(
                    is_valid=False,
                    missing_configs=[],
                    unresolvable_builders=list(config_map.keys()),
                    config_errors={'builder_resolution': [str(e)]},
                    dependency_issues=[],
                    warnings=[]
                )
            
            # Run comprehensive validation
            validation_result = self.validation_engine.validate_dag_compatibility(
                dag_nodes=dag_nodes,
                available_configs=available_configs,
                config_map=config_map,
                builder_registry=builder_map
            )
            
            self.logger.info(f"Validation completed: {validation_result.summary()}")
            return validation_result
            
        except Exception as e:
            self.logger.error(f"Validation failed with error: {e}")
            return ValidationResult(
                is_valid=False,
                missing_configs=[],
                unresolvable_builders=[],
                config_errors={'validation_error': [str(e)]},
                dependency_issues=[],
                warnings=[]
            )
    
    def preview_resolution(self, dag: PipelineDAG) -> ResolutionPreview:
        """
        Preview how DAG nodes will be resolved to configs and builders.
        
        Returns a detailed preview showing:
        - Node → Configuration mappings
        - Configuration → Step Builder mappings
        - Detected step types and dependencies
        - Potential issues or ambiguities
        
        Args:
            dag: PipelineDAG instance to preview
            
        Returns:
            ResolutionPreview with detailed resolution information
        """
        try:
            self.logger.info(f"Previewing resolution for {len(dag.nodes)} DAG nodes")
            
            # Create a template using our create_template method
            temp_template = self.create_template(dag)
            
            # Get preview data
            dag_nodes = list(dag.nodes)
            available_configs = temp_template.configs
            
            # Get metadata from template if available
            metadata = None
            if hasattr(temp_template, '_loaded_metadata'):
                metadata = temp_template._loaded_metadata
            
            # Get resolution candidates
            preview_data = self.config_resolver.preview_resolution(
                dag_nodes=dag_nodes,
                available_configs=available_configs,
                metadata=metadata
            )
            
            # Build preview result
            node_config_map = {}
            config_builder_map = {}
            resolution_confidence = {}
            ambiguous_resolutions = []
            recommendations = []
            
            for node, candidates in preview_data.items():
                if candidates:
                    best_candidate = candidates[0]
                    config_type = best_candidate['config_type']
                    confidence = best_candidate['confidence']
                    
                    node_config_map[node] = config_type
                    resolution_confidence[node] = confidence
                    
                    # Get builder for this config type
                    try:
                        step_type = self.builder_registry._config_class_to_step_type(config_type)
                        builder_class = self.builder_registry.get_builder_for_step_type(step_type)
                        config_builder_map[config_type] = builder_class.__name__
                    except Exception:
                        config_builder_map[config_type] = "UNKNOWN"
                    
                    # Check for ambiguity
                    if len(candidates) > 1 and abs(candidates[0]['confidence'] - candidates[1]['confidence']) < 0.1:
                        ambiguous_resolutions.append(f"{node} has {len(candidates)} similar candidates")
                    
                    # Add recommendations for low confidence
                    if confidence < 0.8:
                        recommendations.append(f"Consider renaming '{node}' for better matching")
                else:
                    node_config_map[node] = "UNRESOLVED"
                    resolution_confidence[node] = 0.0
                    recommendations.append(f"Add configuration for node '{node}'")
            
            preview = ResolutionPreview(
                node_config_map=node_config_map,
                config_builder_map=config_builder_map,
                resolution_confidence=resolution_confidence,
                ambiguous_resolutions=ambiguous_resolutions,
                recommendations=recommendations
            )
            
            self.logger.info("Resolution preview completed successfully")
            return preview
            
        except Exception as e:
            self.logger.error(f"Failed to generate resolution preview: {e}")
            # Return empty preview with error
            return ResolutionPreview(
                node_config_map={},
                config_builder_map={},
                resolution_confidence={},
                ambiguous_resolutions=[],
                recommendations=[f"Preview failed: {str(e)}"]
            )
    
    def compile(self, dag: PipelineDAG, pipeline_name: Optional[str] = None, **kwargs) -> Pipeline:
        """
        Compile DAG to pipeline with full control.
        
        Args:
            dag: PipelineDAG instance to compile
            pipeline_name: Optional pipeline name override
            **kwargs: Additional arguments for template
            
        Returns:
            Generated SageMaker Pipeline
            
        Raises:
            PipelineAPIError: If compilation fails
        """
        try:
            self.logger.info(f"Compiling DAG with {len(dag.nodes)} nodes to pipeline")
            
            # Reuse our create_template method but enforce skip_validation=True for performance
            # as the validation is typically done separately before compilation
            template_kwargs = {**self.template_kwargs, **kwargs}
            template_kwargs['skip_validation'] = True  # Skip validation for performance during direct compilation
            
            template = self.create_template(dag, **template_kwargs)
            
            # Build pipeline
            pipeline = template.generate_pipeline()
            
            # Override pipeline name if provided or generate a new one
            if pipeline_name:
                pipeline.name = pipeline_name
            else:
                # Import here to avoid circular import
                from .name_generator import generate_pipeline_name
                
                # Get base_config from template
                base_config = template.base_config
                base_name = getattr(base_config, 'pipeline_name', 'mods')
                version = getattr(base_config, 'pipeline_version', '1.0')
                # Generate a name using the same approach as PipelineTemplateBase
                pipeline.name = generate_pipeline_name(base_name, version)
            
            self.logger.info(f"Successfully compiled DAG to pipeline: {pipeline.name}")
            return pipeline
            
        except Exception as e:
            self.logger.error(f"Failed to compile DAG to pipeline: {e}")
            raise PipelineAPIError(f"DAG compilation failed: {e}") from e
    
    def compile_with_report(
        self,
        dag: PipelineDAG,
        pipeline_name: Optional[str] = None,
        **kwargs
    ) -> Tuple[Pipeline, ConversionReport]:
        """
        Compile DAG to pipeline and return detailed compilation report.
        
        Args:
            dag: PipelineDAG instance to compile
            pipeline_name: Optional pipeline name override
            **kwargs: Additional arguments for template
            
        Returns:
            Tuple of (Pipeline, ConversionReport)
        """
        try:
            self.logger.info(f"Compiling DAG with detailed reporting")
            
            # Compile pipeline
            pipeline = self.compile(dag, pipeline_name=pipeline_name, **kwargs)
            
            # Generate report
            dag_nodes = list(dag.nodes)
            resolution_details = {}
            total_confidence = 0.0
            warnings = []
            
            # Get resolution preview for report details
            preview = self.preview_resolution(dag)
            
            for node in dag_nodes:
                if node in preview.node_config_map:
                    config_type = preview.node_config_map[node]
                    confidence = preview.resolution_confidence.get(node, 0.0)
                    builder_type = preview.config_builder_map.get(config_type, 'Unknown')
                    
                    resolution_details[node] = {
                        'config_type': config_type,
                        'builder_type': builder_type,
                        'confidence': confidence
                    }
                    
                    total_confidence += confidence
                    
                    if confidence < 0.8:
                        warnings.append(f"Low confidence resolution for node '{node}': {confidence:.2f}")
            
            avg_confidence = total_confidence / len(dag_nodes) if dag_nodes else 0.0
            
            # Add ambiguity warnings
            warnings.extend(preview.ambiguous_resolutions)
            
            report = ConversionReport(
                pipeline_name=pipeline.name,
                steps=dag_nodes,
                resolution_details=resolution_details,
                avg_confidence=avg_confidence,
                warnings=warnings,
                metadata={
                    'dag_nodes': len(dag_nodes),
                    'dag_edges': len(dag.edges),
                    'config_path': self.config_path,
                    'builder_registry_stats': self.builder_registry.get_registry_stats()
                }
            )
            
            self.logger.info(f"Compilation completed with report: {report.summary()}")
            return pipeline, report
            
        except Exception as e:
            self.logger.error(f"Failed to compile DAG with report: {e}")
            raise PipelineAPIError(f"DAG compilation with report failed: {e}") from e
    
    def create_template(self, dag: PipelineDAG, **kwargs) -> "DynamicPipelineTemplate":
        """
        Create a pipeline template from the DAG without generating the pipeline.
        
        This allows inspecting or modifying the template before pipeline generation.
        
        Args:
            dag: PipelineDAG instance to create a template for
            **kwargs: Additional arguments for template
            
        Returns:
            DynamicPipelineTemplate instance ready for pipeline generation
            
        Raises:
            PipelineAPIError: If template creation fails
        """
        try:
            self.logger.info(f"Creating template for DAG with {len(dag.nodes)} nodes")
            
            # Merge kwargs with default values
            template_kwargs = {**self.template_kwargs}
            
            # Set default skip_validation if not provided
            if 'skip_validation' not in kwargs:
                template_kwargs['skip_validation'] = False  # Enable validation by default
            
            # Update with any other kwargs provided
            template_kwargs.update(kwargs)
            
            # Create dynamic template
            template = DynamicPipelineTemplate(
                dag=dag,
                config_path=self.config_path,
                config_resolver=self.config_resolver,
                builder_registry=self.builder_registry,
                sagemaker_session=self.sagemaker_session,
                role=self.role,
                **template_kwargs
            )
            
            self.logger.info(f"Successfully created template")
            return template
            
        except Exception as e:
            self.logger.error(f"Failed to create template: {e}")
            raise PipelineAPIError(f"Template creation failed: {e}") from e
    
    def get_supported_step_types(self) -> list:
        """
        Get list of supported step types.
        
        Returns:
            List of supported step type names
        """
        return self.builder_registry.list_supported_step_types()
    
    def validate_config_file(self) -> Dict[str, Any]:
        """
        Validate the configuration file structure.
        
        Returns:
            Dictionary with validation results
        """
        try:
            # Create a minimal DAG to test config loading
            test_dag = PipelineDAG()
            test_dag.add_node("test_node")
            
            # Use create_template with skip_validation=True to just test config loading
            temp_template = self.create_template(dag=test_dag, skip_validation=True)
            
            configs = temp_template.configs
            
            return {
                'valid': True,
                'config_count': len(configs),
                'config_types': [type(config).__name__ for config in configs.values()],
                'config_names': list(configs.keys())
            }
            
        except Exception as e:
            return {
                'valid': False,
                'error': str(e),
                'config_count': 0,
                'config_types': [],
                'config_names': []
            }
