import numpy as np
import pandas as pd

import pint
import pint_pandas
from ITR.data.osc_units import ureg, Q_, PA_

from typing import List, Type, Dict
from ITR.configs import ColumnsConfig, TemperatureScoreConfig, ProjectionConfig, VariablesConfig
from ITR.data.data_providers import CompanyDataProvider, ProductionBenchmarkDataProvider, \
    IntensityBenchmarkDataProvider

from ITR.interfaces import ICompanyData, EScope, IProductionBenchmarkScopes, IEmissionIntensityBenchmarkScopes, \
    IBenchmark, IProjection, ICompanyEIProjections, ICompanyEIProjectionsScopes, ICompanyProjection, IHistoricEIScopes, \
    IHistoricEmissionsScopes, IProductionRealization


# TODO handling of scopes in benchmarks

class BaseCompanyDataProvider(CompanyDataProvider):
    """
    Data provider skeleton for JSON files parsed by the fastAPI json encoder. This class serves primarily for connecting
    to the ITR tool via API.

    :param companies: A list of ICompanyData objects that each contain fundamental company data
    :param column_config: An optional ColumnsConfig object containing relevant variable names
    :param tempscore_config: An optional TemperatureScoreConfig object containing temperature scoring settings
    """

    def __init__(self,
                 companies: List[ICompanyData],
                 column_config: Type[ColumnsConfig] = ColumnsConfig,
                 tempscore_config: Type[TemperatureScoreConfig] = TemperatureScoreConfig):
        super().__init__()
        self._companies = self._validate_projected_trajectories(companies)
        self.column_config = column_config
        self.temp_config = tempscore_config

    def _validate_projected_trajectories(self, companies: List[ICompanyData]) -> List[ICompanyData]:
        companies_without_data = [c.company_id for c in companies if not c.historic_data and not c.projected_intensities]
        assert not companies_without_data, \
            f"Provide either historic emission data or projections for companies with IDs {companies_without_data}"
        companies_without_projections = [c for c in companies if not c.projected_intensities]
        if companies_without_projections:
            companies_with_projections = [c for c in companies if c.projected_intensities]
            return companies_with_projections + EmissionIntensityProjector().project_intensities(companies_without_projections)
        else:
            return companies

    def _convert_projections_to_series(self, company: ICompanyData, feature: str,
                                       scope: EScope = EScope.S1S2) -> pd.Series:
        """
        extracts the company projected intensities or targets for a given scope
        :param feature: PROJECTED_TRAJECTORIES or PROJECTED_TARGETS (both are intensities)
        :param scope: a scope
        :return: pd.Series
        """
        units = company.dict()[self.column_config.PRODUCTION_METRIC]['units']
        return pd.Series(
            {p['year']: p['value'] for p in company.dict()[feature][str(scope)]['projections'] },
            name=company.company_id, dtype=f'pint[t CO2/{units}]')

    # ??? Why prefer TRAJECTORY over TARGET?
    def _get_company_intensity_at_year(self, year: int, company_ids: List[str]) -> pd.Series:
        """
        Returns projected intensities for a given set of companies and year
        :param year: calendar year
        :param company_ids: List of company ids
        :return: pd.Series with intensities for given company ids
        """
        return self.get_company_projected_trajectories(company_ids)[year]

    def get_company_data(self, company_ids: List[str]) -> List[ICompanyData]:
        """
        Get all relevant data for a list of company ids. This method should return a list of ICompanyData
        instances.

        :param company_ids: A list of company IDs (ISINs)
        :return: A list containing the company data
        """
        company_data = [company for company in self._companies if company.company_id in company_ids]

        if len(company_data) is not len(company_ids):
            missing_ids = [company.company_id for company in self._companies if company.company_id not in company_ids]
            assert not missing_ids, f"Company IDs not found in fundamental data: {missing_ids}"

        return company_data

    def get_value(self, company_ids: List[str], variable_name: str) -> pd.Series:
        """
        Gets the value of a variable for a list of companies ids
        :param company_ids: list of company ids
        :param variable_name: variable name of the projected feature
        :return: series of values
        """
        return self.get_company_fundamentals(company_ids)[variable_name]

    def get_company_intensity_and_production_at_base_year(self, company_ids: List[str]) -> pd.DataFrame:
        """
        overrides subclass method
        :param: company_ids: list of company ids
        :return: DataFrame the following columns :
        ColumnsConfig.COMPANY_ID, ColumnsConfig.PRODUCTION_METRIC, ColumnsConfig.GHG_SCOPE12, ColumnsConfig.BASE_EI,
        ColumnsConfig.SECTOR and ColumnsConfig.REGION
        """
        df_fundamentals = self.get_company_fundamentals(company_ids)
        base_year = self.temp_config.CONTROLS_CONFIG.base_year
        company_info = df_fundamentals.loc[
            company_ids, [self.column_config.SECTOR, self.column_config.REGION,
                          self.column_config.PRODUCTION_METRIC,
                          self.column_config.GHG_SCOPE12]]
        ei_at_base = self._get_company_intensity_at_year(base_year, company_ids).rename(self.column_config.BASE_EI)
        return company_info.merge(ei_at_base, left_index=True, right_index=True)

    def get_company_fundamentals(self, company_ids: List[str]) -> pd.DataFrame:
        """
        :param company_ids: A list of company IDs
        :return: A pandas DataFrame with company fundamental info per company (company_id is a column)
        """
        return pd.DataFrame.from_records(
            [ICompanyData.parse_obj(c.dict()).dict() for c in self.get_company_data(company_ids)],
            exclude=['projected_targets', 'projected_intensities', 'historic_data']).set_index(self.column_config.COMPANY_ID)

    def get_company_projected_trajectories(self, company_ids: List[str]) -> pd.DataFrame:
        """
        :param company_ids: A list of company IDs
        :return: A pandas DataFrame with projected intensity trajectories per company, indexed by company_id
        """
        trajectory_list = [self._convert_projections_to_series(c, self.column_config.PROJECTED_EI) for c in
             self.get_company_data(company_ids)]
        if trajectory_list:
            return pd.DataFrame(trajectory_list)
        return pd.DataFrame()

    def get_company_projected_targets(self, company_ids: List[str]) -> pd.DataFrame:
        """
        :param company_ids: A list of company IDs
        :return: A pandas DataFrame with projected intensity targets per company, indexed by company_id
        """
        target_list = [self._convert_projections_to_series(c, self.column_config.PROJECTED_TARGETS) for c in
             self.get_company_data(company_ids)]
        if target_list:
            return pd.DataFrame(target_list)
        return pd.DataFrame()

# This is actual output production (whatever the output production units may be).
# Not to be confused with the term "projected production" as it relates to energy intensity.

class BaseProviderProductionBenchmark(ProductionBenchmarkDataProvider):

    def __init__(self, production_benchmarks: IProductionBenchmarkScopes,
                 column_config: Type[ColumnsConfig] = ColumnsConfig,
                 tempscore_config: Type[TemperatureScoreConfig] = TemperatureScoreConfig):
        """
        Base provider that relies on pydantic interfaces. Default for FastAPI usage
        :param production_benchmarks: List of IProductionBenchmarkScopes
        :param column_config: An optional ColumnsConfig object containing relevant variable names
        :param tempscore_config: An optional TemperatureScoreConfig object containing temperature scoring settings
        """
        super().__init__()
        self.temp_config = tempscore_config
        self.column_config = column_config
        self._productions_benchmarks = production_benchmarks

    # Note that bencharmk production series are dimensionless.
    def _convert_benchmark_to_series(self, benchmark: IBenchmark) -> pd.Series:
        """
        extracts the company projected intensity or production targets for a given scope
        :param scope: a scope
        :return: pd.Series
        """
        return pd.Series({r.year: r.value for r in benchmark.projections}, name=(benchmark.region, benchmark.sector))

    # Production benchmarks are dimensionless.  S1S2 has nothing to do with any company data.
    # It's a label in the top-level of benchmark data.  Currently S1S2 is the only label with any data.
    def _get_projected_production(self, scope: EScope = EScope.S1S2) -> pd.DataFrame:
        """
        Converts IProductionBenchmarkScopes into dataframe for a scope
        :param scope: a scope
        :return: pd.DataFrame
        """
        result = []
        for bm in self._productions_benchmarks.dict()[str(scope)]['benchmarks']:
            result.append(self._convert_benchmark_to_series(IBenchmark.parse_obj(bm)))
        df_bm = pd.DataFrame(result)
        df_bm.index.names = [self.column_config.REGION, self.column_config.SECTOR]

        return df_bm

    def get_company_projected_production(self, company_sector_region_info: pd.DataFrame) -> pd.DataFrame:
        """
        get the projected productions for list of companies in ghg_scope12
        :param company_sector_region_info: DataFrame with at least the following columns :
        ColumnsConfig.COMPANY_ID, ColumnsConfig.GHG_SCOPE12, ColumnsConfig.SECTOR and ColumnsConfig.REGION
        :return: DataFrame of projected productions for [base_year - base_year + 50]
        """
        benchmark_production_projections = self.get_benchmark_projections(company_sector_region_info)
        company_production = company_sector_region_info[self.column_config.GHG_SCOPE12]
        return benchmark_production_projections.add(1).cumprod(axis=1).mul(
                    company_production, axis=0) # .astype(f"pint[{units}]")

    def get_benchmark_projections(self, company_sector_region_info: pd.DataFrame,
                                  scope: EScope = EScope.S1S2) -> pd.DataFrame:
        """
        Overrides subclass method
        returns a Dataframe with production benchmarks per company_id given a region and sector.
        :param company_sector_region_info: DataFrame with at least the following columns :
        ColumnsConfig.COMPANY_ID, ColumnsConfig.SECTOR and ColumnsConfig.REGION
        :param scope: a scope
        :return: A DataFrame with company and intensity benchmarks per calendar year per row
        """
        benchmark_projection = self._get_projected_production(scope)  # TODO optimize performance
        sectors = company_sector_region_info[self.column_config.SECTOR]
        regions = company_sector_region_info[self.column_config.REGION]
        benchmark_regions = regions.copy()
        mask = benchmark_regions.isin(benchmark_projection.reset_index()[self.column_config.REGION])
        benchmark_regions.loc[~mask] = "Global"

        benchmark_projection = benchmark_projection.loc[list(zip(benchmark_regions, sectors)),
                                                        range(self.temp_config.CONTROLS_CONFIG.base_year,
                                                              self.temp_config.CONTROLS_CONFIG.target_end_year + 1)]
        benchmark_projection.index = sectors.index

        return benchmark_projection


class BaseProviderIntensityBenchmark(IntensityBenchmarkDataProvider):
    def __init__(self, EI_benchmarks: IEmissionIntensityBenchmarkScopes,
                 column_config: Type[ColumnsConfig] = ColumnsConfig,
                 tempscore_config: Type[TemperatureScoreConfig] = TemperatureScoreConfig):
        super().__init__(EI_benchmarks.benchmark_temperature, EI_benchmarks.benchmark_global_budget,
                         EI_benchmarks.is_AFOLU_included)
        self._EI_benchmarks = EI_benchmarks
        self.temp_config = tempscore_config
        self.column_config = column_config

    def get_SDA_intensity_benchmarks(self, company_info_at_base_year: pd.DataFrame) -> pd.DataFrame:
        """
        Overrides subclass method
        returns a Dataframe with intensity benchmarks per company_id given a region and sector.
        :param company_info_at_base_year: DataFrame with at least the following columns :
        ColumnsConfig.COMPANY_ID, ColumnsConfig.BASE_EI, ColumnsConfig.SECTOR and ColumnsConfig.REGION
        :return: A DataFrame with company and SDA intensity benchmarks per calendar year per row
        """
        intensity_benchmarks = self._get_intensity_benchmarks(company_info_at_base_year)
        decarbonization_paths = self._get_decarbonizations_paths(intensity_benchmarks)
        last_ei = intensity_benchmarks[self.temp_config.CONTROLS_CONFIG.target_end_year]
        ei_base = company_info_at_base_year[self.column_config.BASE_EI]
        df = decarbonization_paths.mul((ei_base - last_ei), axis=0)
        df = df.add(last_ei, axis=0).astype(ei_base.dtype)
        return df

    def _get_decarbonizations_paths(self, intensity_benchmarks: pd.DataFrame) -> pd.DataFrame:
        """
        Overrides subclass method
        Returns a DataFrame with the projected decarbonization paths for the supplied companies in intensity_benchmarks.
        :param: A DataFrame with company and intensity benchmarks per calendar year per row
        :return: A pd.DataFrame with company and decarbonisation path s per calendar year per row
        """
        return intensity_benchmarks.apply(lambda row: self._get_decarbonization(row), axis=1)

    def _get_decarbonization(self, intensity_benchmark_row: pd.Series) -> pd.Series:
        """
        Overrides subclass method
        returns a Series with the decarbonization path for a benchmark.
        :param: A Series with a company's intensity benchmarks per calendar year per row
        :return: A pd.Series with a company's decarbonisation paths per calendar year per row
        """
        first_ei = intensity_benchmark_row[self.temp_config.CONTROLS_CONFIG.base_year]
        last_ei = intensity_benchmark_row[self.temp_config.CONTROLS_CONFIG.target_end_year]
        # This throws a warning when processing a NaN
        return intensity_benchmark_row.apply(lambda x: (x.m - last_ei.m) / (first_ei.m - last_ei.m))

    def _convert_benchmark_to_series(self, benchmark: IBenchmark) -> pd.Series:
        """
        extracts the company projected intensities or targets for a given scope
        :param scope: a scope
        :return: pd.Series
        """
        return pd.Series({p.year: p.value for p in benchmark.projections}, name=(benchmark.region, benchmark.sector), dtype=f'pint[{benchmark.benchmark_metric.units}]')

    def _get_projected_intensities(self, scope: EScope = EScope.S1S2) -> pd.DataFrame:
        """
        Converts IEmissionIntensityBenchmarkScopes into dataframe for a scope
        :param scope: a scope
        :return: pd.DataFrame
        """
        result = []
        for bm in self._EI_benchmarks.dict()[str(scope)]['benchmarks']:
            result.append(self._convert_benchmark_to_series(IBenchmark.parse_obj(bm)))
        df_bm = pd.DataFrame(result)
        df_bm.index.names = [self.column_config.REGION, self.column_config.SECTOR]
        return df_bm

    def _get_intensity_benchmarks(self, company_sector_region_info: pd.DataFrame,
                                  scope: EScope = EScope.S1S2) -> pd.DataFrame:
        """
        Overrides subclass method
        returns a Dataframe with production benchmarks per company_id given a region and sector.
        :param company_sector_region_info: DataFrame with at least the following columns :
        ColumnsConfig.COMPANY_ID, ColumnsConfig.SECTOR and ColumnsConfig.REGION
        :param scope: a scope
        :return: A DataFrame with company and intensity benchmarks per calendar year per row
        """
        benchmark_projection = self._get_projected_intensities(scope)  # TODO optimize performance
        sectors = company_sector_region_info[self.column_config.SECTOR]
        regions = company_sector_region_info[self.column_config.REGION]
        benchmark_regions = regions.copy()
        mask = benchmark_regions.isin(benchmark_projection.reset_index()[self.column_config.REGION])
        benchmark_regions.loc[~mask] = "Global"

        benchmark_projection = benchmark_projection.loc[list(zip(benchmark_regions, sectors)),
                                                        range(self.temp_config.CONTROLS_CONFIG.base_year,
                                                              self.temp_config.CONTROLS_CONFIG.target_end_year + 1)]
        benchmark_projection.index = sectors.index
        return benchmark_projection


class EmissionIntensityProjector(object):
    """
    This class projects emission intensities on company level based on historic data on:
    - A company's emission history (in t CO2)
    - A company's production history (units depend on industry, e.g. TWh for electricity)
    """

    def __init__(self):
        pass

    def project_intensities(self, companies: List[ICompanyData]) -> List[ICompanyData]:
        historic_data = self._extract_historic_data(companies)
        self._compute_missing_historic_emission_intensities(companies, historic_data)

        historic_years = [column for column in historic_data.columns if type(column) == int]
        projection_years = range(max(historic_years), ProjectionConfig.TARGET_YEAR)
        # historic_intensities.loc[historic_intensities.index.get_level_values('company_id')=='US6293775085']
        
        historic_intensities = historic_data[historic_years]
        standardized_intensities = self._standardize(historic_intensities)
        intensity_trends = self._get_trends(standardized_intensities)
        extrapolated = self._extrapolate(intensity_trends, projection_years, historic_data)

        self._add_projections_to_companies(companies, extrapolated)
        return companies

    def _extract_historic_data(self, companies: List[ICompanyData]) -> pd.DataFrame:
        data = []
        for company in companies:
            if not company.historic_data:
                continue
            if company.historic_data.productions:
                data.append(self._historic_productions_to_dict(company.company_id, company.historic_data.productions))
            if company.historic_data.emissions:
                data.extend(self._historic_emissions_to_dicts(company.company_id, company.historic_data.emissions))
            if company.historic_data.emission_intensities:
                data.extend(self._historic_emission_intensities_to_dicts(company.company_id,
                                                                         company.historic_data.emission_intensities))
        if not data:
            print("No historic data anywhere")
            print(companies)
        return pd.DataFrame.from_records(data).set_index(
            [ColumnsConfig.COMPANY_ID, ColumnsConfig.VARIABLE, ColumnsConfig.SCOPE])

    def _historic_productions_to_dict(self, id: str, productions: List[IProductionRealization]) -> Dict[str, str]:
        prods = {prod['year']: prod['value'] for prod in productions}
        return {ColumnsConfig.COMPANY_ID: id, ColumnsConfig.VARIABLE: VariablesConfig.PRODUCTIONS,
                ColumnsConfig.SCOPE: 'Production', **prods}

    def _historic_emissions_to_dicts(self, id: str, emission_scopes: IHistoricEmissionsScopes) -> List[Dict[str, str]]:
        data = []
        for scope, emissions in emission_scopes.dict().items():
            if emissions:
                ems = {em['year']: em['value'] for em in emissions}
                data.append({ColumnsConfig.COMPANY_ID: id, ColumnsConfig.VARIABLE: VariablesConfig.EMISSIONS,
                             ColumnsConfig.SCOPE: scope, **ems})
        return data

    def _historic_emission_intensities_to_dicts(self, id: str, intensities_scopes: IHistoricEIScopes) \
            -> List[Dict[str, str]]:
        data = []
        for scope, intensities in intensities_scopes.dict().items():
            if intensities:
                intsties = {intsty['year']: intsty['value'] for intsty in intensities}
                data.append({ColumnsConfig.COMPANY_ID: id, ColumnsConfig.VARIABLE: VariablesConfig.EMISSION_INTENSITIES,
                             ColumnsConfig.SCOPE: scope, **intsties})
        return data

    def _compute_missing_historic_emission_intensities(self, companies, historic_data):
        scopes = EScope.get_scopes()
        missing_data = []
        for company in companies:
            # Create keys to index historic_data DataFrame for readability
            production_key = (company.company_id, VariablesConfig.PRODUCTIONS, 'Production')
            emission_keys = {scope: (company.company_id, VariablesConfig.EMISSIONS, scope) for scope in scopes}
            ei_keys = {scope: (company.company_id, VariablesConfig.EMISSION_INTENSITIES, scope) for scope in scopes}
            for scope in scopes:
                if ei_keys[scope] not in historic_data.index:  # Emission intensities not yet computed for this scope
                    if scope == 'S1S2':
                        try:  # Try to add S1 and S2 emission intensities
                            historic_data.loc[ei_keys[scope]] = historic_data.loc[ei_keys['S1']] + \
                                                                historic_data.loc[ei_keys['S2']]
                        except KeyError:  # Either S1 or S2 emission intensities not readily available
                            try:  # Try to compute S1+S2 EIs from S1+S2 emissions and productions
                                historic_data.loc[ei_keys[scope]] = historic_data.loc[emission_keys[scope]] / \
                                                                    historic_data.loc[production_key]
                            except KeyError:
                                missing_data.append(f"{company.company_id} - {scope}")
                    elif scope == 'S1S2S3':  # Implement when S3 data is available
                        pass
                    elif scope == 'S3':  # Remove when S3 data is available - will be handled by 'else'
                        pass
                    else:  # S1 and S2 cannot be computed from other EIs, so use emissions and productions
                        try:
                            historic_data.loc[ei_keys[scope]] = historic_data.loc[emission_keys[scope]] / \
                                                                historic_data.loc[production_key]
                        except KeyError:
                            missing_data.append(f"{company.company_id} - {scope}")
        assert not missing_data, f"Provide either historic emission intensity data, or historic emission and " \
                                 f"production data for these company - scope combinations: {missing_data}"

    def _add_projections_to_companies(self, companies: List[ICompanyData], extrapolations: pd.DataFrame):
        for company in companies:
            results = extrapolations.loc[(company.company_id, VariablesConfig.EMISSION_INTENSITIES, 'S1S2')]
            if company.production_metric:
                # These are already stored in the correct compact format
                units = f"t CO2/{company.production_metric.units}"
            elif company.sector=='Steel':
                units = "t CO2/Fe_ton"
            elif company.sector=='Electricity Utilities':
                units = "Mt CO2/GJ"
            try:
                projections = [IProjection(year=int(year), value=Q_(value, units)) for year, value in results.items()
                               if year >= TemperatureScoreConfig.CONTROLS_CONFIG.base_year]
            except:
                pass
            company.projected_intensities = ICompanyEIProjectionsScopes(
                S1S2=ICompanyEIProjections(projections=projections)
            )

    def _standardize(self, intensities: pd.DataFrame) -> pd.DataFrame:
        # When columns are years and rows are all different intensity types, we cannot winsorize
        # Transpose the dataframe, winsorize the columns (which are all coherent because they belong to a single variable/company), then transpose again
        intensities = intensities.T
        for col in intensities.columns:
            s = intensities[col]
            if s.notnull().any():
                try:
                    intensities[col] = s.astype(f"pint[{s.loc[s.first_valid_index()].u:~P}]")
                except:
                    # Don't remember why this was needed, but theory is "no harm, no foul"
                    pass
        winsorized_intensities: pd.DataFrame = self._winsorize(intensities)
        standardized_intensities: pd.DataFrame = self._interpolate(winsorized_intensities)
        return standardized_intensities.T

    def _winsorize(self, historic_intensities: pd.DataFrame) -> pd.DataFrame:
        winsorized: pd.DataFrame = historic_intensities.clip(
            lower=historic_intensities.quantile(q=ProjectionConfig.LOWER_PERCENTILE, axis='index', numeric_only=True),
            upper=historic_intensities.quantile(q=ProjectionConfig.UPPER_PERCENTILE, axis='index', numeric_only=True),
            axis='columns'
        )
        return winsorized

    def _interpolate(self, historic_intensities: pd.DataFrame) -> pd.DataFrame:
        # Interpolate NaNs surrounded by values, and extrapolate NaNs with last known value
        interpolated = historic_intensities.copy()
        for col in interpolated.columns:
            if interpolated[col].isnull().all():
                continue
            qty = interpolated[col].values.quantity
            s = pd.Series(data=qty.m, index=interpolated.index)
            interpolated[col] = pd.Series(PA_(s.interpolate(method='linear', inplace=False, limit_direction='forward'), f"{qty.u:~P}"), index=interpolated.index)
        return interpolated

    def _get_trends(self, intensities: pd.DataFrame):
        # Compute year-on-year growth ratios of emission intensities
        
        # Transpose so we can work with homogeneous units in columns.  This means rows are years.
        # pd.Series(intensities.iloc[:,0].values.quantity.m).rolling(window=2, axis='index', closed='right').apply(func=self._year_on_year_ratio, raw=True)
        intensities = intensities.T
        for col in intensities.columns:
            # ratios are dimensionless, so get rid of units, which confuse rolling/apply.  Some columns are NaN-only
            intensities[col] = intensities[col].map(lambda x: x if isinstance(x, float) else x.m)
        ratios: pd.DataFrame = intensities.rolling(window=2, axis='index', closed='right') \
            .apply(func=self._year_on_year_ratio, raw=True)

        trends: pd.DataFrame = ratios.median(axis='index', skipna=True).clip(
            lower=ProjectionConfig.LOWER_DELTA,
            upper=ProjectionConfig.UPPER_DELTA,
        )
        return trends.T

    def _extrapolate(self, trends: pd.DataFrame, projection_years: range, historic_data: pd.DataFrame) -> pd.DataFrame:
        projected_intensities = historic_data.copy()
        for year in projection_years:
            projected_intensities[year + 1] = projected_intensities[year] * (1 + trends)
        return projected_intensities

    def _year_on_year_ratio(self, arr: np.ndarray) -> float:
        return (arr[1] / arr[0]) - 1.0
