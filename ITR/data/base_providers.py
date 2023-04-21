import warnings  # needed until quantile behaves better with Pint quantities in arrays
import numpy as np
import pandas as pd
import pint
from pint import DimensionalityError
import pydantic
from pydantic import ValidationError

from functools import reduce, partial
from operator import add
from typing import List, Type, Dict

import ITR
from ITR.data.osc_units import ureg, Q_, PA_, asPintDataFrame, asPintSeries, PintType, EI_Metric

from ITR.configs import ColumnsConfig, VariablesConfig, ProjectionControls, LoggingConfig

import logging
logger = logging.getLogger(__name__)
LoggingConfig.add_config_to_logger(logger)

from ITR.data.data_providers import CompanyDataProvider, ProductionBenchmarkDataProvider, \
    IntensityBenchmarkDataProvider
from ITR.interfaces import ICompanyData, EScope, IProductionBenchmarkScopes, IEIBenchmarkScopes, \
    IBenchmark, IProjection, ICompanyEIProjections, ICompanyEIProjectionsScopes, IHistoricEIScopes, \
    IHistoricEmissionsScopes, IProductionRealization, ITargetData, IHistoricData, ICompanyEIProjection, \
    IEmissionRealization, IEIRealization, DF_ICompanyEIProjections
from ITR.interfaces import EI_Quantity


# TODO handling of scopes in benchmarks


# The benchmark projected production format is based on year-over-year growth and starts out like this:

#                                                2019     2020            2049       2050
# region                 sector        scope                    ...                                                  
# Steel                  Global        AnyScope   0.0   0.00306  ...     0.0155     0.0155
#                        Europe        AnyScope   0.0   0.00841  ...     0.0155     0.0155
#                        North America AnyScope   0.0   0.00748  ...     0.0155     0.0155
# Electricity Utilities  Global        AnyScope   0.0    0.0203  ...     0.0139     0.0139
#                        Europe        AnyScope   0.0    0.0306  ...   -0.00113   -0.00113
#                        North America AnyScope   0.0    0.0269  ...   0.000426   0.000426
# etc.

# To compute the projected production for a company in given sector/region, we need to start with the
# base_year_production for that company and apply the year-over-year changes projected by the benchmark
# until all years are computed.  We need to know production of each year, not only the final year
# because the cumumulative emissions of the company will be the sum of the emissions of each year,
# which depends on both the production projection (computed here) and the emissions intensity projections
# (computed elsewhere).

# Let Y2019 be the production of a company in 2019.
# Y2020 = Y2019 + (Y2019 * df_pp[2020]) = Y2019 + Y2019 * (1.0 + df_pp[2020])
# Y2021 = Y2020 + (Y2020 * df_pp[2020]) = Y2020 + Y2020 * (1.0 + df_pp[2021])
# etc.

# The Pandas `cumprod` function calculates precisely the cumulative product we need
# As the math shows above, the terms we need to accumulate are 1.0 + growth.

# df.add(1).cumprod(axis=1).astype('pint[]') results in a project that looks like this:
# 
#                                                2019     2020  ...      2049      2050
# region                 sector        scope                    ...                    
# Steel                  Global        AnyScope   1.0  1.00306  ...  1.419076  1.441071
#                        Europe        AnyScope   1.0  1.00841  ...  1.465099  1.487808
#                        North America AnyScope   1.0  1.00748  ...  1.457011  1.479594
# Electricity Utilities  Global        AnyScope   1.0  1.02030  ...  2.907425  2.947838
#                        Europe        AnyScope   1.0  1.03060  ...  1.751802  1.749822
#                        North America AnyScope   1.0  1.02690  ...  2.155041  2.155959
# etc.

class BaseProviderProductionBenchmark(ProductionBenchmarkDataProvider):

    def __init__(self, production_benchmarks: IProductionBenchmarkScopes,
                 column_config: Type[ColumnsConfig] = ColumnsConfig):
        """
        Base provider that relies on pydantic interfaces. Default for FastAPI usage
        :param production_benchmarks: List of IProductionBenchmarkScopes
        :param column_config: An optional ColumnsConfig object containing relevant variable names
        """
        super().__init__()
        self.column_config = column_config
        self._productions_benchmarks = production_benchmarks
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                # TODO: sort out why this trips up pint
                self._prod_delta_df = pd.DataFrame([self._convert_benchmark_to_series(bm, EScope.AnyScope) for bm in self._productions_benchmarks[EScope.AnyScope.name].benchmarks])
        except AttributeError:
            assert False
        self._prod_delta_df.index.names = [self.column_config.SECTOR, self.column_config.REGION, self.column_config.SCOPE]
        # See comment above to understand use of `cumprod` function
        self._prod_df = self._prod_delta_df.add(1.0).cumprod(axis=1).astype('pint[]')

    # Note that benchmark production series are dimensionless.
    # FIXME: They also don't need a scope.  Remove scope when we change IBenchmark format...
    def _convert_benchmark_to_series(self, benchmark: IBenchmark, scope: EScope) -> pd.Series:
        """
        extracts the company projected intensity or production targets for a given scope
        :param scope: a scope
        :return: pd.Series
        """
        units = str(benchmark.benchmark_metric)
        # Benchmarks don't need work-around for https://github.com/hgrecco/pint/issues/1687, but if they did:
        # units = ureg.parse_units(benchmark.benchmark_metric)
        years, values = list(map(list, zip(*{r.year: r.value.to(units).m for r in benchmark.projections}.items())))
        return pd.Series(PA_(values, dtype=units),
                         index = years, name=(benchmark.sector, benchmark.region, scope))

    # Production benchmarks are dimensionless, relevant for AnyScope
    def _get_projected_production(self, scope: EScope = EScope.AnyScope) -> pd.DataFrame:
        """
        Converts IProductionBenchmarkScopes into dataframe for a scope
        :param scope: a scope
        :return: pd.DataFrame
        """
        return self._prod_df
    
        # The call to this function generates a 42-row (and counting...) DataFrame for the one row we're going to end up needing...
        df_bm = pd.DataFrame([self._convert_benchmark_to_series(bm, scope) for bm in self._productions_benchmarks[scope.name].benchmarks])
        df_bm.index.names = [self.column_config.SECTOR, self.column_config.REGION, self.column_config.SCOPE]
        
        df_partial_pp = df_bm.add(1).cumprod(axis=1).astype('pint[]')

        return df_partial_pp

    def get_company_projected_production(self, company_sector_region_scope: pd.DataFrame) -> pd.DataFrame:
        """
        get the projected productions for list of companies
        :param company_sector_region_scope: DataFrame with at least the following columns :
        ColumnsConfig.COMPANY_ID, ColumnsConfig.SECTOR, ColumnsConfig.REGION, ColumnsConfig.SCOPE
        :return: DataFrame of projected productions for [base_year through 2050]
        """
        # get_benchmark_projections is an expensive call.  It's designed to return ALL benchmark info for ANY sector/region combo passed
        # and it does all that work whether we need all the data or just one row.  Best to lift this out of any inner loop
        # and use the valuable DataFrame it creates.
        company_benchmark_projections = self.get_benchmark_projections(company_sector_region_scope)
        company_production = company_sector_region_scope.set_index(self.column_config.SCOPE, append=True)[self.column_config.BASE_YEAR_PRODUCTION]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            nan_production = company_production.map(ITR.isnan)
            if nan_production.any():
                # If we don't have valid production data for base year, we get back a nan result that's a pain to debug, so nag here
                logger.error(f"these companies are missing production data: {nan_production[nan_production].index.get_level_values(0).to_list()}")
            # We transpose the operation so that Pandas is happy to preserve the dtype integrity of the column
            company_projected_productions_t = company_benchmark_projections.T.mul(company_production, axis=1)
            return company_projected_productions_t.T

    def get_benchmark_projections(self, company_sector_region_scope: pd.DataFrame, scope: EScope = EScope.AnyScope) -> pd.DataFrame:
        """
        Overrides subclass method
        returns a Dataframe with production benchmarks per company_id given a region and sector.
        :param company_sector_region_scope: DataFrame indexed by ColumnsConfig.COMPANY_ID
        with at least the following columns: ColumnsConfig.SECTOR, ColumnsConfig.REGION, and ColumnsConfig.SCOPE
        :param scope: a scope
        :return: An all-quantified DataFrame with intensity benchmark data per calendar year per row, indexed by company.
        """

        benchmark_projection = self._get_projected_production(scope)  # TODO optimize performance
        df = (company_sector_region_scope[['sector', 'region', 'scope']]
              .reset_index()
              .drop_duplicates()
              .set_index(['company_id', 'scope']))
        # We drop the meaningless S1S2/AnyScope from the production benchmark and replace it with the company's scope.
        # This is needed to make indexes align when we go to multiply production times intensity for a scope.
        company_benchmark_projections = df.merge(benchmark_projection.droplevel('scope'),
                                                 left_on=['sector', 'region'], right_index=True, how='left')
        mask = company_benchmark_projections.iloc[:, -1].isna()
        if mask.any():
            # Patch up unknown regions as "Global"
            global_benchmark_projections = df[mask].merge(benchmark_projection.loc[(slice(None), 'Global'), :].droplevel(['region','scope']),
                                                          left_on=['sector'], right_index=True, how='left').drop(columns='region')
            combined_benchmark_projections = pd.concat([company_benchmark_projections[~mask].drop(columns='region'),
                                                        global_benchmark_projections])
            return combined_benchmark_projections.drop(columns='sector')
        return company_benchmark_projections.drop(columns=['sector', 'region'])


class BaseProviderIntensityBenchmark(IntensityBenchmarkDataProvider):
    def __init__(self, EI_benchmarks: IEIBenchmarkScopes,
                 column_config: Type[ColumnsConfig] = ColumnsConfig,
                 projection_controls: ProjectionControls = ProjectionControls()):
        super().__init__(EI_benchmarks.benchmark_temperature, EI_benchmarks.benchmark_global_budget,
                         EI_benchmarks.is_AFOLU_included)
        self._EI_benchmarks = EI_benchmarks
        self.column_config = column_config
        self.projection_controls = projection_controls
        benchmarks_as_series = []
        for scope_name in EScope.get_scopes():
            try:
                for bm in EI_benchmarks[scope_name].benchmarks:
                    benchmarks_as_series.append(self._convert_benchmark_to_series(bm, EScope[scope_name]))
            except AttributeError:
                pass

        with warnings.catch_warnings():
            # pd.DataFrame.__init__ (in pandas/core/frame.py) ignores the beautiful dtype information adorning the pd.Series list elements we are providing.  Sad!
            warnings.simplefilter("ignore")
            self._EI_df = pd.DataFrame(benchmarks_as_series).sort_index()
        self._EI_df.index.names = [self.column_config.SECTOR, self.column_config.REGION, self.column_config.SCOPE]
        

    # SDA stands for Sectoral Decarbonization Approach; see https://sciencebasedtargets.org/resources/files/SBTi-Power-Sector-15C-guide-FINAL.pdf
    def get_SDA_intensity_benchmarks(self, company_info_at_base_year: pd.DataFrame, scope_to_calc: EScope = None) -> pd.DataFrame:
        """
        Overrides subclass method
        returns a Dataframe with intensity benchmarks per company_id given a region and sector.
        :param company_info_at_base_year: DataFrame with at least the following columns :
        ColumnsConfig.COMPANY_ID, ColumnsConfig.BASE_EI, ColumnsConfig.SECTOR, ColumnsConfig.REGION, ColumnsConfig.SCOPE
        :return: A DataFrame with company and SDA intensity benchmarks per calendar year per row
        """
        # To make pint happier, we do our math in columns that can be represented by PintArrays
        intensity_benchmarks_t = self._get_intensity_benchmarks(company_info_at_base_year,
                                                                scope_to_calc)
        decarbonization_paths_t = self._get_decarbonizations_paths(intensity_benchmarks_t)
        last_ei = intensity_benchmarks_t.loc[self.projection_controls.TARGET_YEAR]
        ei_base = intensity_benchmarks_t.loc[self.projection_controls.BASE_YEAR]
        df_t = decarbonization_paths_t.mul((ei_base - last_ei), axis=1)
        df_t = df_t.add(last_ei, axis=1)
        df_t.index.name = 'year'
        idx = pd.Index.intersection(df_t.columns,
                                    pd.MultiIndex.from_arrays([company_info_at_base_year.index,
                                                               company_info_at_base_year.scope]))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # pint units don't like being twisted from columns to rows, but it's ok
            df = df_t[idx].T
        return df

    def _get_decarbonizations_paths(self, intensity_benchmarks_t: pd.DataFrame) -> pd.DataFrame:
        """
        Overrides subclass method
        Returns a DataFrame with the projected decarbonization paths for the supplied companies in intensity_benchmarks.
        :param: A DataFrame with company and intensity benchmarks per calendar year per row
        :return: A pd.DataFrame with company and decarbonisation path s per calendar year per row
        """
        return intensity_benchmarks_t.apply(lambda col: self._get_decarbonization(col))

    def _get_decarbonization(self, intensity_benchmark_ser: pd.Series) -> pd.Series:
        """
        Overrides subclass method
        returns a Series with the decarbonization path for a benchmark.
        :param: A Series with a company's intensity benchmarks per calendar year per row
        :return: A pd.Series with a company's decarbonisation paths per calendar year per row
        """
        last_ei = intensity_benchmark_ser[self.projection_controls.TARGET_YEAR]
        ei_diff = intensity_benchmark_ser[self.projection_controls.BASE_YEAR] - last_ei
        # TODO: does this still throw a warning when processing a NaN?  convert to base units before accessing .magnitude
        return (intensity_benchmark_ser - last_ei) / ei_diff

    def _convert_benchmark_to_series(self, benchmark: IBenchmark, scope: EScope) -> pd.Series:
        """
        extracts the company projected intensities or targets for a given scope
        :param scope: a scope
        :return: pd.Series
        """
        s = pd.Series({p.year: p.value for p in benchmark.projections
                       if p.year in range(self.projection_controls.BASE_YEAR,self.projection_controls.TARGET_YEAR+1)},
                      name=(benchmark.sector, benchmark.region, scope),
                      dtype=f'pint[{str(benchmark.benchmark_metric)}]')
        return s

    def _get_intensity_benchmarks(self, company_sector_region_scope: pd.DataFrame, scope_to_calc: EScope = None) -> pd.DataFrame:
        """
        Overrides subclass method
        returns a Dataframe with intensity benchmarks per company_id given a region and sector.
        :param company_sector_region_scope: DataFrame indexed by ColumnsConfig.COMPANY_ID
        with at least the following columns: ColumnsConfig.SECTOR, ColumnsConfig.REGION, and ColumnsConfig.SCOPE
        :return: A DataFrame with company and intensity benchmarks; rows are calendar years, columns are company data
        """
        benchmark_projections = self._EI_df[self._EI_df.columns[self._EI_df.columns.isin(range(self.projection_controls.BASE_YEAR,self.projection_controls.TARGET_YEAR+1))]]
        df = company_sector_region_scope[['sector', 'region', 'scope']]
        if scope_to_calc is not None:
            df = df[df.scope.eq(scope_to_calc)]

        df = df.join(benchmark_projections, on=['sector','region','scope'], how='left')
        mask = df.iloc[:, -1].isna()
        if mask.any():
            # We have request for benchmark data for either regions or scopes we don't have...
            # Resetting the index gives us row numbers useful for editing DataFrame with fallback data
            df = df.reset_index()
            mask = df.iloc[:, -1].isna()
            benchmark_global = benchmark_projections.loc[:, 'Global', :]
            # DF1 selects all global data matching sector and scope...
            df1 = df[mask].iloc[:, 0:4].join(benchmark_global, on=['sector','scope'], how='inner')
            # ...which we can then mark as 'Global'
            df1.region = 'Global'
            df.loc[df1.index, :] = df1
            # Remove any NaN rows from DF we could not update
            mask1 = df.iloc[:, -1].isna()
            df2 = df[~mask1]
            # Restore the COMPANY_ID index; we no longer need row numbers to keep edits straight
            company_benchmark_projections = df2.set_index('company_id')
        else:
            company_benchmark_projections = df
        company_benchmark_projections.set_index('scope', append=True, inplace=True)
        # Drop SECTOR and REGION as the result will be used by math functions operating across the whole DataFrame
        return asPintDataFrame(company_benchmark_projections.drop(['sector', 'region'], axis=1).T)


class BaseCompanyDataProvider(CompanyDataProvider):
    """
    Data provider skeleton for JSON files parsed by the fastAPI json encoder. This class serves primarily for connecting
    to the ITR tool via API.

    :param companies: A list of ICompanyData objects that each contain fundamental company data
    :param column_config: An optional ColumnsConfig object containing relevant variable names
    :param projection_controls: An optional ProjectionControls object containing projection settings
    """

    def __init__(self,
                 companies: List[ICompanyData],
                 column_config: Type[ColumnsConfig] = ColumnsConfig,
                 projection_controls: ProjectionControls = ProjectionControls()):
        super().__init__()
        self.column_config = column_config
        self.projection_controls = projection_controls
        self.missing_ids = set([])
        # In the initialization phase, `companies` has minimal fundamental values (company_id, company_name, sector, region,
        # but not projected_intensities, projected_targets, etc)
        self._companies = companies
        # Initially we don't have to do any allocation of emissions across multiple sectors, but if we do, we'll update the index here.
        self._bm_allocation_index = pd.DataFrame().index

    def _validate_projected_trajectories(self, companies: List[ICompanyData], df_bm_ei: pd.DataFrame) -> List[ICompanyData]:
        """
        Called when benchmark data is first known, or when projection control parameters or benchmark data changes.
        COMPANIES are a list of companies with historic data that need to be projected.
        DF_BM_EI is bemchmark data that is needed only for normalizing EI_METRICs of the projections.
        In previous incarnations of this function, no benchmark data was needed for any reason.
        """
        companies_without_data = [c.company_id for c in companies if
                                  not c.historic_data and not c.projected_intensities]
        if companies_without_data:
            error_message = f"Provide either historic emission data or projections for companies with " \
                            f"IDs {companies_without_data}"
            logger.error(error_message)
            raise ValueError(error_message)
        companies_without_historic_data = [c for c in companies if not c.historic_data]
        if companies_without_historic_data:
            # Can arise from degenerate test cases
            pass
        base_year = self.projection_controls.BASE_YEAR
        for company in companies_without_historic_data:
            scope_em = {}
            scope_ei = {}
            if company.projected_intensities:
                for scope_name in EScope.get_scopes():
                    if isinstance(company.projected_intensities[scope_name], DF_ICompanyEIProjections):
                        scope_ei[scope_name] = [ IEIRealization(year=base_year, value=company.projected_intensities[scope_name].projections[base_year]) ]
                    elif company.projected_intensities[scope_name] is None:
                        scope_ei[scope_name] = []
                    else:
                        # Should not be reached, but this gives right answer if it is.
                        scope_ei[scope_name] = [ eir.value for eir in company.projected_intensities[scope_name].projections if eir.year==base_year ]
                scope_em = { scope: [IEmissionRealization(year=base_year, value=ei[0].value * company.base_year_production)] if ei else []
                             for scope, ei in scope_ei.items() }
            else:
                scope_em['S1'] = scope_em['S2'] = []
                scope_em['S3'] = [IEmissionRealization(year=base_year, value=company.ghg_s3)] if company.ghg_s3 else []
                scope_em['S1S2'] = [IEmissionRealization(year=base_year, value=company.ghg_s1s2)]
                scope_em['S1S2S3'] = [IEmissionRealization(year=base_year, value=company.ghg_s1s2+company.ghg_s3)] if company.ghg_s1s2 and company.ghg_s3 else []
                scope_ei = { scope: [IEIRealization(year=base_year, value=em[0].value / company.base_year_production)] if em else []
                             for scope, em in scope_em.items() }
            company.historic_data = IHistoricData(
                productions=[IProductionRealization(year=base_year, value=company.base_year_production)],
                emissions=IHistoricEmissionsScopes(**scope_em),
                emissions_intensities=IHistoricEIScopes(**scope_ei))
        companies_without_projections = [c for c in companies if not c.projected_intensities]
        if companies_without_projections:
            companies_with_projections = [c for c in companies if c.projected_intensities]
            companies = companies_with_projections + EITrajectoryProjector(self.projection_controls).project_ei_trajectories(
                companies_without_projections)
        # Normalize all intensity metrics to match benchmark intensity metrics (as much as we can)
        logger.info("Normalizing intensity metrics")
        for company in companies:
            sector = company.sector
            region = company.region
            if (sector, region) in df_bm_ei.index:
                # FIXME: if we change _EI_df to columnar data we could pick up units from dtype
                ei_metric = str(df_bm_ei.loc[(sector, region)].iat[0, 0].u)
            elif (sector, 'Global') in df_bm_ei.index:
                ei_metric = str(df_bm_ei.loc[(sector, 'Global')].iat[0, 0].u)
            else:
                continue
            for scope in EScope.get_scopes():
                if company.projected_intensities[scope]:
                    try:
                        setattr (company.projected_intensities, scope,
                                 DF_ICompanyEIProjections(ei_metric=ei_metric, projections=company.projected_intensities[scope].projections.astype(f"pint[{ei_metric}]")))
                    except DimensionalityError:
                        logger.error(f"intensity values for company {company.company_id} not compatible with benchmark ({ei_metric})")
                        break
        logger.info("Done normalizing intensity metrics")
        return companies

    # Because this presently defaults to S1S2 always, targets spec'd for S1 only, S2 only, or S1+S2+S3 are not well-handled.
    def _convert_projections_to_series(self, company: ICompanyData, feature: str,
                                       scope: EScope = EScope.S1S2) -> pd.Series:
        """
        extracts the company projected intensities or targets for a given scope
        :param feature: PROJECTED_TRAJECTORIES or PROJECTED_TARGETS (both are intensities)
        :param scope: a scope
        :return: pd.Series
        """
        company_dict = company.dict()
        production_units = str(company_dict[self.column_config.PRODUCTION_METRIC])
        emissions_units = str(company_dict[self.column_config.EMISSIONS_METRIC])

        if company_dict[feature][scope.name]:
            # Simple case: just one scope
            projections = company_dict[feature][scope.name]['projections']
            if isinstance(projections, pd.Series):
                # FIXME: should do this upstream somehow
                projections.name = (company.company_id, scope)
                return projections.loc[pd.Index(range(self.projection_controls.BASE_YEAR,self.projection_controls.TARGET_YEAR+1))]
            return pd.Series(
                {p['year']: p['value'] for p in projections if p['year'] in range(self.projection_controls.BASE_YEAR,self.projection_controls.TARGET_YEAR+1)},
                name=(company.company_id, scope), dtype=f'pint[{emissions_units}/({production_units})]')
        else:
            assert False
            # Complex case: S1+S2 or S1+S2+S3...we really don't handle yet
            scopes = [EScope[s] for s in scope.value.split('+')]
            projection_scopes = {s: company_dict[feature][s]['projections'] for s in scopes if company_dict[feature][s.name]}
            if len(projection_scope_names) > 1:
                projection_series = {}
                for s in scopes:
                    projection_series[s] = pd.Series(
                        {p['year']: p['value'] for p in company_dict[feature][s.name]['projections']
                         if p['year'] in range(self.projection_controls.BASE_YEAR,self.projection_controls.TARGET_YEAR+1)},
                        name=(company.company_id, s), dtype=f'pint[{emissions_units}/({production_units})]')
                series_adder = partial(pd.Series.add, fill_value=0)
                res = reduce(series_adder, projection_series.values())
                return res
            elif len(projection_scopes) == 0:
                return pd.Series(
                    {year: np.nan for year in range(self.historic_years[-1] + 1, self.projection_controls.TARGET_YEAR + 1)},
                    name=company.company_id, dtype=f'pint[{emissions_units}/({production_units})]'
                )
            else:
                projections = company_dict[feature][list(projection_scopes.keys())[0]]['projections']

    def _calculate_target_projections(self, production_bm: BaseProviderProductionBenchmark, ei_bm: BaseProviderIntensityBenchmark = None):
        """
        We cannot calculate target projections until after we have loaded benchmark data.
        We do so when companies are associated with benchmarks, in the DataWarehouse construction
        
        :param production_bm: A Production Benchmark (multi-sector, single-scope, 2020-2050)
        :param ei_bm: Intensity Benchmarks for all sectors and scopes defined by the benchmark, 2020-2050
        """        
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # FIXME: Note that we don't need to call with a scope, because production is independent of scope.
            # We use the arbitrary EScope.AnyScope just to be explicit about that.
            df_partial_pp = production_bm._get_projected_production(EScope.AnyScope)

        for c in self._companies:
            if c.projected_targets is not None:
                continue
            if c.target_data is None:
                logger.warning(f"No target data for {c.company_name}")
                c.projected_targets = ICompanyEIProjectionsScopes()
            else:
                base_year_production = next((p.value for p in c.historic_data.productions if
                                             p.year == self.projection_controls.BASE_YEAR), None)
                try:
                    co_cumprod = df_partial_pp.loc[c.sector, c.region, EScope.AnyScope] * base_year_production
                except KeyError:
                    # FIXME: Should we fix region info upstream when setting up comopany data?
                    co_cumprod = df_partial_pp.loc[c.sector, "Global", EScope.AnyScope] * base_year_production
                try:
                    if ei_bm:
                        if (c.sector, c.region) in ei_bm._EI_df.index:
                            ei_df = ei_bm._EI_df.loc[(c.sector, c.region)]
                        elif (c.sector, "Global") in ei_bm._EI_df.index:
                            ei_df = ei_bm._EI_df.loc[(c.sector, "Global")]
                        else:
                            logger.error(f"company {c.company_name} with ID {c.company_id} sector={c.sector} region={c.region} not in EI benchmark")
                            ei_df = None
                    else:
                        ei_df = None
                    c.projected_targets = EITargetProjector(self.projection_controls).project_ei_targets(c, co_cumprod, ei_df)
                except Exception as err:
                    import traceback
                    logger.error(f"While calculating target projections for {c.company_id}, raised {err} (possible intensity vs. absolute unit mis-match?)")
                    traceback.print_exc()
                    logger.info("Continuing from _calculate_target_projections...")
                    c.projected_targets = ICompanyEIProjectionsScopes()
    
    # ??? Why prefer TRAJECTORY over TARGET?
    def _get_company_intensity_at_year(self, year: int, company_ids: List[str]) -> pd.Series:
        """
        Returns projected intensities for a given set of companies and year
        :param year: calendar year
        :param company_ids: List of company ids
        :return: pd.Series with intensities for given company ids
        """
        return self.get_company_projected_trajectories(company_ids, year=year)

    def get_company_data(self, company_ids: List[str]) -> List[ICompanyData]:
        """
        Get all relevant data for a list of company ids. This method should return a list of ICompanyData
        instances.

        :param company_ids: A list of company IDs (ISINs)
        :return: A list containing the company data
        """
        company_data = [company for company in self._companies if company.company_id in company_ids]

        if len(company_data) is not len(company_ids):
            self.missing_ids.update(set([c_id for c_id in company_ids if c_id not in [c.company_id for c in company_data]]))
            logger.warning(f"Companies not found in fundamental data and excluded from further computations: "
                           f"{self.missing_ids}")

        return company_data

    def get_value(self, company_ids: List[str], variable_name: str) -> pd.Series:
        """
        Gets the value of a variable for a list of companies ids
        :param company_ids: list of company ids
        :param variable_name: variable name of the projected feature
        :return: series of values
        """
        # FIXME: this is an expensive operation as it converts all fields in the model just to get a single VARIABLE_NAME
        return self.get_company_fundamentals(company_ids)[variable_name]

    def get_company_intensity_and_production_at_base_year(self, company_ids: List[str]) -> pd.DataFrame:
        """
        overrides subclass method
        :param: company_ids: list of company ids
        :return: DataFrame the following columns :
        ColumnsConfig.COMPANY_ID, ColumnsConfig.PRODUCTION_METRIC, ColumnsConfig.BASE_EI,
        ColumnsConfig.SECTOR, ColumnsConfig.REGION, ColumnsConfig.SCOPE,
        ColumnsConfig.GHG_SCOPE12, ColumnsConfig.GHG_SCOPE3
        
        The BASE_EI column is for the scope in the SCOPE column.
        """
        # FIXME: this creates an untidy data mess.  GHG_SCOPE12 and GHG_SCOPE3 are anachronisms.
        # company_data = self.get_company_data(company_ids)
        df_fundamentals = self.get_company_fundamentals(company_ids)
        base_year = self.projection_controls.BASE_YEAR
        company_info = df_fundamentals.loc[
            company_ids, [self.column_config.SECTOR, self.column_config.REGION,
                          self.column_config.BASE_YEAR_PRODUCTION,
                          self.column_config.GHG_SCOPE12,
                          self.column_config.GHG_SCOPE3]]
        # Do rely on getting info from projections; Don't grovel through historic data instead
        ei_at_base = self._get_company_intensity_at_year(base_year, company_ids).rename(self.column_config.BASE_EI)
        # historic_ei = { (company.company_id, scope): { self.column_config.BASE_EI: eir.value }
        #                 for scope in EScope.get_result_scopes()
        #                 for company in company_data
        #                 for eir in getattr(company.historic_data.emissions_intensities, scope.name)
        #                 if eir.year==base_year }
        # 
        # ei_at_base = pd.DataFrame.from_dict(historic_ei, orient='index')
        # ei_at_base.index.names=['company_id', 'scope']
        df = company_info.merge(ei_at_base, left_index=True, right_index=True)
        df.reset_index('scope', inplace=True)
        cols = df.columns.tolist()        
        df = df[cols[1:3] + [cols[0]] + cols[3:]]
        return df

    def get_company_fundamentals(self, company_ids: List[str]) -> pd.DataFrame:
        """
        :param company_ids: A list of company IDs
        :return: A pandas DataFrame with company fundamental info per company (company_id is a column)
        """
        excluded_cols = ['projected_targets', 'projected_intensities', 'historic_data', 'target_data']
        df = pd.DataFrame.from_records(
            [dict(ICompanyData.parse_obj({k:v for k, v in dict(c).items() if k not in excluded_cols}))
             for c in self.get_company_data(company_ids)]).set_index(self.column_config.COMPANY_ID)
        return df

    def get_company_projected_trajectories(self, company_ids: List[str], year=None) -> pd.DataFrame:
        """
        :param company_ids: A list of company IDs
        :param year: values for a specific year, or all years if None
        :return: A pandas DataFrame with projected intensity trajectories per company, indexed by company_id and scope
        """
        company_ids, scopes, projections = list(
            map(list, zip(*[ (c.company_id, EScope[scope_name], c.projected_intensities[scope_name].projections)
                             # FIXME: we should make _companies a dict so we can look things up rather than searching every time!
                             for c in self._companies for scope_name in EScope.get_scopes()
                             if c.company_id in company_ids
                             if c.projected_intensities[scope_name] ])) )
        if projections:
            index=pd.MultiIndex.from_tuples(zip(company_ids, scopes), names=["company_id", "scope"])
            if year is not None:
                if isinstance(projections[0], ICompanyEIProjectionsScopes):
                    values = [yvp.value for yvp in pt if yvp.year==year for pt in projections]
                else:
                    values = list( map(lambda x: x[year].squeeze(), projections) )
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    # pint units don't like columns of heterogeneous data...tough!
                    return pd.Series(data=values, index=index, name=year)
            else:
                if isinstance(projections[0], ICompanyEIProjectionsScopes):
                    values = [{yvp.year:yvp.value for yvp in pt} for pt in projections]
                else:
                    values = projections
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    # FIXME: why cannot Pint and Pandas agree to make a nice DF from a list of PintArray Series?
                    return pd.DataFrame(data=values, index=index)
        return pd.DataFrame()

    def get_company_projected_targets(self, company_ids: List[str], year=None) -> pd.DataFrame:
        """
        :param company_ids: A list of company IDs
        :param year: values for a specific year, or all years if None
        :return: A pandas DataFrame with projected intensity targets per company, indexed by company_id
        """
        # Tempting as it is to follow the pattern of constructing the same way we create `projected_trajectories`
        # targets are trickier because they have ragged left edges that want to fill with NaNs when put into DataFrames.
        # _convert_projections_to_series has the nice side effect that PintArrays produce NaNs with units.
        # So if we just need a year from this dataframe, we compute the whole dataframe and return one column.
        # Feel free to write a better implementation if you have time!
        target_list = [self._convert_projections_to_series(c, self.column_config.PROJECTED_TARGETS, EScope[scope_name])
                       for c in self.get_company_data(company_ids)
                       for scope_name in EScope.get_scopes()
                       if c.projected_targets and c.projected_targets[scope_name]]
        if target_list:
            with warnings.catch_warnings():
                # pd.DataFrame.__init__ (in pandas/core/frame.py) ignores the beautiful dtype information adorning the pd.Series list elements we are providing.  Sad!
                warnings.simplefilter("ignore")
                # If target_list produces a ragged left edge, resort columns so that earliest year is leftmost
                df = pd.DataFrame(target_list).sort_index(axis=1)
                df.index.set_names(['company_id', 'scope'], inplace=True)
                if year is not None:
                    return df[year]
                return df
        return pd.DataFrame()


class EIProjector(object):
    """
    This class implements generic projection functions used for both trajectory and target projection.
    """

    def __init__(self, projection_controls: ProjectionControls = ProjectionControls()):
        self.projection_controls = projection_controls

    def _get_bounded_projections(self, results):
        if isinstance(results, list):
            projections = [projection for projection in results
                           if projection.year in range(self.projection_controls.BASE_YEAR, self.projection_controls.TARGET_YEAR+1)]
        else:
            projections = [ICompanyEIProjection(year=year, value=value) for year, value in results.items()
                           if year in range(self.projection_controls.BASE_YEAR, self.projection_controls.TARGET_YEAR+1)]
        return projections


class EITrajectoryProjector(EIProjector):
    """
    This class projects emissions intensities on company level based on historic data on:
    - A company's emission history (in t CO2)
    - A company's production history (units depend on industry, e.g. TWh for electricity)

    It returns the full set of both historic emissions intensities and projected emissions intensities.
    """

    def __init__(self, projection_controls: ProjectionControls = ProjectionControls()):
        super().__init__(projection_controls=projection_controls)

    def project_ei_trajectories(self, companies: List[ICompanyData], backfill_needed=True) -> List[ICompanyData]:
        historic_df = self._extract_historic_df(companies)
        # This modifies historic_df in place...which feeds the intensity extrapolations below
        self._compute_missing_historic_ei(companies, historic_df)
        historic_years = [column for column in historic_df.columns if type(column) == int]
        projection_years = range(max(historic_years), self.projection_controls.TARGET_YEAR+1)
        with warnings.catch_warnings():
            # Don't worry about warning that we are intentionally dropping units as we transpose
            warnings.simplefilter("ignore")
            historic_ei_t = asPintDataFrame(
                historic_df[historic_years].query(f"variable=='{VariablesConfig.EMISSIONS_INTENSITIES}'").T).pint.dequantify()
            historic_ei_t.index.name = 'year'
        if backfill_needed:
            # Fill in gaps between BASE_YEAR and the first data we have
            if ITR.HAS_UNCERTAINTIES:
                backfilled_t = historic_ei_t.apply(lambda col: (lambda fvi: col if fvi is None else col.where(col.index.get_level_values('year') >= fvi, col[fvi]))
                                                            (col.map(lambda x: x.n if isinstance(x, ITR.UFloat) else x).first_valid_index()))
            else:
                backfilled_t = historic_ei_t.apply(lambda col: col.fillna(method='bfill'))
            # FIXME: this hack causes backfilling only on dates on or after the first year of the benchmark, which keeps it from disrupting current test cases
            # while also working on real-world use cases.  But we need to formalize this decision.
            backfilled_t = backfilled_t.reset_index()
            backfilled_t = backfilled_t.where(backfilled_t.year >= self.projection_controls.BASE_YEAR, historic_ei_t.reset_index())
            backfilled_t.set_index('year', inplace=True)
            if not historic_ei_t.compare(backfilled_t).empty:
                logger.warning(f"some data backfilled to {self.projection_controls.BASE_YEAR} for company_ids in list {historic_ei_t.compare(backfilled_t).columns.get_level_values('company_id').unique().tolist()}")
                historic_ei_t = backfilled_t.sort_index(axis=1)
                for company in companies:
                    if company.ghg_s3 is None or ITR.isnan(company.ghg_s3):
                        try:
                            idx = (company.company_id, 'Emissions Ei', EScope.S3)
                            company.ghg_s3 = Q_(historic_ei_t[idx].loc[self.projection_controls.BASE_YEAR].squeeze(),
                                                historic_ei_t[idx].columns[0]) * company.base_year_production
                        except KeyError:
                            # If it's not there, we'll complain later
                            pass
                    if company.ghg_s1s2 is None or ITR.isnan(company.ghg_s1s2):
                        try:
                            idx = (company.company_id, 'Emissions Intensities', EScope.S1S2)
                            company.ghg_s1s2 = Q_(historic_ei_t[idx].loc[self.projection_controls.BASE_YEAR].squeeze(),
                                                  historic_ei_t[idx].columns[0]) * company.base_year_production
                        except KeyError:
                            # If it's not there, we'll complain later
                            pass
        standardized_ei_t = self._standardize(historic_ei_t)
        intensity_trends_t = self._get_trends(standardized_ei_t)
        extrapolated_t = self._extrapolate(intensity_trends_t, projection_years, historic_ei_t)
        # Restrict projection to benchmark years
        extrapolated_t = extrapolated_t[extrapolated_t.index >= self.projection_controls.BASE_YEAR]
        # Restore row-wise shape of DataFrame
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # pint units don't like being twisted from columns to rows, but it's ok
            self._add_projections_to_companies(companies, extrapolated_t.pint.quantify())
        return companies

    def _extract_historic_df(self, companies: List[ICompanyData]) -> pd.DataFrame:
        data = []
        for company in companies:
            if not company.historic_data:
                continue
            if company.historic_data.productions:
                data.append(self._historic_productions_to_dict(company.company_id, company.historic_data.productions))
            if company.historic_data.emissions:
                data.extend(self._historic_emissions_to_dicts(company.company_id, company.historic_data.emissions))
            if company.historic_data.emissions_intensities:
                data.extend(self._historic_ei_to_dicts(company.company_id,
                                                       company.historic_data.emissions_intensities))
        if not data:
            logger.error(f"No historic data for companies: {[c.company_id for c in companies]}")
            raise ValueError("No historic data anywhere")
        df = pd.DataFrame.from_records(data).set_index(
            [ColumnsConfig.COMPANY_ID, ColumnsConfig.VARIABLE, ColumnsConfig.SCOPE])
        # Note that the first valid index may well be Quantity with a NaN value--that's fine
        # We just need to fill in the pure NaNs that arise from very ragged data
        
        with warnings.catch_warnings():
            # TODO: need to investigate whether there is a more sane way to avoid unit warnings
            warnings.simplefilter("ignore")
            df_first_valid = df.apply(lambda x: x[x.first_valid_index()], axis=1)
        df_filled = df.fillna(df.apply(lambda x: df_first_valid.map(lambda y: Q_(np.nan, y.u))))
        return df_filled

    def _historic_productions_to_dict(self, id: str, productions: List[IProductionRealization]) -> Dict[str, str]:
        prods = {prod.year: prod.value for prod in productions}
        return {ColumnsConfig.COMPANY_ID: id, ColumnsConfig.VARIABLE: VariablesConfig.PRODUCTIONS,
                ColumnsConfig.SCOPE: 'Production', **prods}

    def _historic_emissions_to_dicts(self, id: str, emissions_scopes: IHistoricEmissionsScopes) -> List[Dict[str, str]]:
        data = []
        for scope, emissions in dict(emissions_scopes).items():
            if emissions:
                ems = {em['year']: em['value'] for em in emissions}
                data.append({ColumnsConfig.COMPANY_ID: id, ColumnsConfig.VARIABLE: VariablesConfig.EMISSIONS,
                             ColumnsConfig.SCOPE: EScope[scope], **ems})
        return data

    def _historic_ei_to_dicts(self, id: str, intensities_scopes: IHistoricEIScopes) \
            -> List[Dict[str, str]]:
        data = []
        for scope, intensities in dict(intensities_scopes).items():
            if intensities:
                intsties = {intsty['year']: intsty['value'] for intsty in intensities}
                data.append(
                    {ColumnsConfig.COMPANY_ID: id, ColumnsConfig.VARIABLE: VariablesConfig.EMISSIONS_INTENSITIES,
                     ColumnsConfig.SCOPE: EScope[scope], **intsties})
        return data

    # Each benchmark defines its own scope requirements on a per-sector/per-region basis.
    # The purpose of this function is not to infer scope data that might be interesting,
    # but rather to impute the scope data that is actually required, no more, no less.
    def _compute_missing_historic_ei(self, companies: List[ICompanyData], historic_df: pd.DataFrame):
        scopes = [EScope[scope_name] for scope_name in EScope.get_scopes()]
        missing_data = []
        for company in companies:
            # Create keys to index historic_df DataFrame for readability
            production_key = (company.company_id, VariablesConfig.PRODUCTIONS, 'Production')
            emissions_keys = {scope: (company.company_id, VariablesConfig.EMISSIONS, scope) for scope in scopes}
            ei_keys = {scope: (company.company_id, VariablesConfig.EMISSIONS_INTENSITIES, scope) for scope in scopes}
            this_missing_data = []
            append_this_missing_data = True
            for scope in scopes:
                if ei_keys[scope] in historic_df.index:
                    assert company.historic_data.emissions_intensities[scope.name]
                    append_this_missing_data = False
                    continue
                # Emissions intensities not yet computed for this scope
                try:  # All we will try is computing EI from Emissions / Production
                    historic_df.loc[ei_keys[scope]] = (
                        historic_df.loc[emissions_keys[scope]] / historic_df.loc[production_key])
                    append_this_missing_data = False
                except KeyError:
                    this_missing_data.append(f"{company.company_id} - {scope.name}")
                    continue
                # Note that we don't actually add new-found data to historic data...only the starting point for projections
            # This only happens if ALL scope data is missing.  If ANY scope data is present, we'll work with what we get.
            if this_missing_data and append_this_missing_data:
                missing_data.extend(this_missing_data)
        if missing_data:
            error_message = f"Provide either historic emissions intensity data, or historic emission and " \
                            f"production data for these company - scope combinations: {missing_data}"
            logger.error(error_message)
            raise ValueError(error_message)

    def _add_projections_to_companies(self, companies: List[ICompanyData], extrapolations_t: pd.DataFrame):
        for company in companies:
            scope_projections = {}
            scope_dfs = {}
            scope_names = EScope.get_scopes()
            for scope_name in scope_names:
                if not company.historic_data.emissions_intensities or not company.historic_data.emissions_intensities[scope_name]:
                    scope_projections[scope_name] = None
                    continue
                results = extrapolations_t[(company.company_id, VariablesConfig.EMISSIONS_INTENSITIES, EScope[scope_name])]
                if not isinstance(results.dtype, PintType):
                    if results.isna().all():
                        # Pure NaN results (not Quantity(nan)) likely constructed from degenerate test case
                        scope_projections[scope_name] = None
                        continue
                    assert False
                # FIXME: is it OK to discard purely NAN results (and change the testsuite accordingly)?
                # if results.isna().all():
                #     scope_projections[scope_name] = None
                #     continue
                units = f"{results.dtype.units:~P}"
                scope_dfs[scope_name] = results
                try:
                    scope_projections[scope_name] = ICompanyEIProjections(ei_metric=units,
                                                                          projections=self._get_bounded_projections(results))
                except ValidationError:
                    logger.error(f"invalid emissions intensity units {units} given for company {company.company_id} ({company.company_name})")
                    raise
            if scope_projections['S1'] and scope_projections['S2'] and not scope_projections['S1S2']:
                results = scope_dfs['S1'] + scope_dfs['S2']
                units = f"{results.values[0].u:~P}"
                scope_dfs['S1S2'] = results
                scope_projections['S1S2'] = ICompanyEIProjections(ei_metric=units,
                                                                  projections=self._get_bounded_projections(results))
            if scope_projections['S1S2'] and scope_projections['S3'] and not scope_projections['S1S2S3']:
                results = scope_dfs['S1S2'] + scope_dfs['S3']
                units = f"{results.values[0].u:~P}"
                # We don't need to compute scope_dfs['S1S2S3'] because nothing further depends on accessing it here
                scope_projections['S1S2S3'] = ICompanyEIProjections(ei_metric=units,
                                                                    projections=self._get_bounded_projections(results))
            company.projected_intensities = ICompanyEIProjectionsScopes(**scope_projections)

    def _standardize(self, intensities_t: pd.DataFrame) -> pd.DataFrame:
        # At the starting point, we expect that if we have S1, S2, and S1S2 intensities, that S1+S2 = S1S2
        # After winsorization, this is no longer true, because S1 and S2 will be clipped differently than S1S2.

        # It is very convenient to integrate interpolation (which only works on numeric datatypes, not
        # quantities and not uncertainties) with the winsorization process.  So there's no separate
        # _interpolate method.
        winsorized_intensities_t: pd.DataFrame = self._winsorize(intensities_t)
        return winsorized_intensities_t

    def _winsorize(self, historic_intensities: pd.DataFrame) -> pd.DataFrame:
        # quantile doesn't handle pd.NA inside Quantity; FIXME: we can use np.nan because not expecting UFloat in input data

        # Turns out we have to dequantify here: https://github.com/pandas-dev/pandas/issues/45968
        # Can try again when ExtensionArrays are supported by `quantile`, `clip`, and friends
        if ITR.HAS_UNCERTAINTIES:
            try:
                nominal_intensities = historic_intensities.apply(lambda col: pd.Series(ITR.nominal_values(col), index=col.index, name=col.name))
                uncertain_intensities = historic_intensities.apply(lambda col: pd.Series(ITR.std_devs(col), index=col.index, name=col.name))
            except ValueError:
                logger.error(f"ValueError in _winsorize")
                raise
        else:
            # pint.dequantify did all the hard work for us
            nominal_intensities = historic_intensities
        # See https://github.com/hgrecco/pint-pandas/issues/114
        lower=nominal_intensities.quantile(q=self.projection_controls.LOWER_PERCENTILE, axis='index', numeric_only=False)
        upper=nominal_intensities.quantile(q=self.projection_controls.UPPER_PERCENTILE, axis='index', numeric_only=False)
        winsorized: pd.DataFrame = nominal_intensities.clip(
            lower=lower,
            upper=upper,
            axis='columns'
        )

        if ITR.HAS_UNCERTAINTIES:
            # FIXME: the clipping process can properly introduce uncertainties.  The low and high values that are clipped could be
            # replaced by the clipped values +/- the lower and upper percentile values respectively.
            wnominal_values = winsorized.apply(lambda col: col.interpolate(method='linear', inplace=False, limit_direction='forward', limit_area='inside'))
            uwinsorized = wnominal_values.combine(uncertain_intensities, ITR.recombine_nom_and_std)
            return uwinsorized

        # FIXME: If we have S1, S2, and S1S2 intensities, should we treat winsorized(S1)+winsorized(S2) as winsorized(S1S2)?
        # FIXME: If we have S1S2 (or S1 and S2) and S3 and S1S23 intensities, should we treat winsorized(S1S2)+winsorized(S3) as winsorized(S1S2S3)?
        return winsorized

    def _interpolate(self, historic_intensities_t: pd.DataFrame) -> pd.DataFrame:
        # Interpolate NaNs surrounded by values, but don't extrapolate NaNs with last known value
        raise NotImplementedError


    def _get_trends(self, intensities_t: pd.DataFrame):
        # FIXME: rolling windows require conversion to float64.  Don't want to be a nuisance...
        if ITR.HAS_UNCERTAINTIES:
            intensities_t = intensities_t.apply(lambda col: ITR.nominal_values(col))
        ratios_t: pd.DataFrame = intensities_t.rolling(window=2, axis='index', closed='right') \
                                              .apply(func=self._year_on_year_ratio, raw=True)

        # # Add weight to trend movements across multiple years (normalized to year-over-year, not over two years...)
        # # FIXME: we only want to do this for median, not mean.
        # if self.projection_controls.TREND_CALC_METHOD==pd.DataFrame.median:
        #     ratios_2 = ratios
        #     ratios_3: pd.DataFrame = intensities.rolling(window=3, axis='index', closed='right') \
        #         .apply(func=self._year_on_year_ratio, raw=True).div(2.0)
        #     ratios = pd.concat([ratios_2, ratios_3])
        # elif self.projection_controls.TREND_CALC_METHOD==pd.DataFrame.mean:
        #     pass
        # else:
        #     raise ValueError("Unhanlded TREND_CALC_METHOD")

        trends_t: pd.DataFrame = self.projection_controls.TREND_CALC_METHOD(ratios_t, axis='index', skipna=True).clip(
            lower=self.projection_controls.LOWER_DELTA,
            upper=self.projection_controls.UPPER_DELTA,
        )
        return trends_t

    def _extrapolate(self, trends_t: pd.Series, projection_years: range, historic_intensities_t: pd.DataFrame) -> pd.DataFrame:
        historic_intensities_t = historic_intensities_t[historic_intensities_t.columns.intersection(trends_t.index)]
        # We need to do a mini-extrapolation if we don't have complete historic data

        def _extrapolate_mini(col, trend):
            col_na = col.map(ITR.isnan)
            col_na_idx = col_na[col_na].index
            last_valid = col[~col_na].tail(1)
            mini_trend = pd.Series([trend + 1] * len(col_na[col_na]), index=col_na_idx, dtype='float64').cumprod()
            col.loc[col_na_idx] = last_valid.squeeze() * mini_trend
            return col

        historic_intensities_t = historic_intensities_t.apply(lambda col: _extrapolate_mini(col, trends_t[col.name]))

        # Now the big extrapolation
        projected_intensities_t = (pd.concat([trends_t.add(1.0)] * len(projection_years[1:]), axis=1).T
                                   .cumprod()
                                   .rename(index=dict(zip(range(0, len(projection_years[1:])), projection_years[1:])))
                                   .mul(historic_intensities_t.iloc[-1], axis=1))

        # Clean up rows by converting NaN/None into Quantity(np.nan, unit_type)
        columnwise_intensities_t = pd.concat([historic_intensities_t, projected_intensities_t])
        columnwise_intensities_t.index.name = 'year'
        return columnwise_intensities_t

    # Might return a float, might return a ufloat
    def _year_on_year_ratio(self, arr: np.ndarray):
        # Subsequent zeroes represent no year-on-year change
        if arr[0]==0.0 and arr[-1]==0.0:
            return 0.0
        # Due to rounding, we might overshoot the zero target and go negative
        # So round the negative number to zero and treat it as a 100% year-on-year decline
        if arr[0]>=0.0 and arr[-1]<=0.0:
            return -1.0
        return (arr[-1] / arr[0]) - 1.0


class EITargetProjector(EIProjector):
    """
    This class projects emissions intensities from a company's targets and historic data. Targets are specified per
    scope in terms of either emissions or emission intensity reduction. Interpolation between last known historic data
    and (a) target(s) is CAGR-based, but not entirely CAGR (beacuse zero can only be approached asymptotically
    and any CAGR that approaches zero in finite time must have extraordinarily steep initial drop, which is unrealistic).

    Remember that pd.Series are always well-behaved with pint[] quantities.  pd.DataFrame columns are well-behaved,
    but data across columns is not always well-behaved.  We therefore make this function assume we are projecting targets
    for a specific company, in a specific sector.  If we want to project targets for multiple sectors, we have to call it multiple times.
    This function doesn't need to know what sector it's computing for...only tha there is only one such, for however many scopes.
    """

    def __init__(self, projection_controls: ProjectionControls = ProjectionControls()):
        self.projection_controls = projection_controls

    def _order_scope_targets(self, scope_targets):
        if not scope_targets:
            # Nothing to do
            return scope_targets
        # If there are multiple targets that land on the same year for the same scope, choose the most recently set target
        unique_target_years = [(target.target_end_year, target.target_start_year) for target in scope_targets]
        # This sorts targets into ascending target years and descending start years
        unique_target_years.sort(key=lambda t: (t[0], -t[1]))
        # Pick the first target year most recently articulated, preserving ascending order of target yeares
        unique_target_years = [(uk, next(v for k, v in unique_target_years if k == uk)) for uk in
                               dict(unique_target_years).keys()]
        # Now use those pairs to select just the targets we want
        unique_scope_targets = [unique_targets[0] for unique_targets in \
                                [[target for target in scope_targets if
                                  (target.target_end_year, target.target_start_year) == u] \
                                 for u in unique_target_years]]
        unique_scope_targets.sort(key=lambda target: (target.target_end_year))

        # We only trust the most recently communicated netzero target, but prioritize the most recently communicated, most aggressive target
        netzero_scope_targets = [target for target in unique_scope_targets if target.netzero_year]
        netzero_scope_targets.sort(key=lambda t: (-t.target_start_year, t.netzero_year))
        if netzero_scope_targets:
            netzero_year = netzero_scope_targets[0].netzero_year
            for target in unique_scope_targets:
                target.netzero_year = netzero_year
        return unique_scope_targets

    def calculate_nz_target_years(self, targets: List[ITargetData]) -> dict:
        """Input:
        @target: A list of stated carbon reduction targets
        @returns: A dict of SCOPE_NAME: NETZERO_YEAR pairs
        """
        # We first try to find the earliest netzero year target for each scope
        nz_target_years = {'S1': 9999, 'S2': 9999, 'S1S2': 9999, 'S3': 9999, 'S1S2S3': 9999}
        for target in targets:
            scope_name = target.target_scope.name
            if target.netzero_year < nz_target_years[scope_name]:
                nz_target_years[scope_name] = target.netzero_year
            if target.target_reduction_pct == 1.0 and target.target_end_year < nz_target_years[scope_name]:
                nz_target_years[scope_name] = target.target_end_year

        # We then infer netzero year targets for constituents of compound scopes from compound scopes
        # and infer netzero year taregts for compound scopes as the last of all constituents
        if nz_target_years['S1S2S3'] < nz_target_years['S1S2']:
            logger.warn(f"target S1S2S3 date <= S1S2 date")
            nz_target_years['S1S2'] = nz_target_years['S1S2S3']
        nz_target_years['S1'] = min(nz_target_years['S1S2'], nz_target_years['S1'])
        nz_target_years['S2'] = min(nz_target_years['S1S2'], nz_target_years['S2'])
        nz_target_years['S1S2'] = min(nz_target_years['S1S2'], max(nz_target_years['S1'], nz_target_years['S2']))
        nz_target_years['S3'] = min(nz_target_years['S1S2S3'], nz_target_years['S3'])
        # nz_target_years['S1S2'] and nz_target_years['S3'] must both be <= nz_target_years['S1S2S3'] at this point
        nz_target_years['S1S2S3'] = max(nz_target_years['S1S2'], nz_target_years['S3'])
        return {scope_name: nz_year if nz_year<9999 else None for scope_name, nz_year in nz_target_years.items()}

    def _get_ei_projections_from_ei_realizations(self, ei_realizations, i):
        for j in range(0,i+1):
            if ei_realizations[j].year >= self.projection_controls.BASE_YEAR and not ITR.isnan(ei_realizations[j].value.m):
                break
        model_ei_projections = [ICompanyEIProjection(year=ei_realizations[k].year, value=ei_realizations[k].value)
                                # NaNs in the middle may still be a problem!
                                for k in range(j,i+1)]
        while model_ei_projections[0].year > self.projection_controls.BASE_YEAR:
            model_ei_projections = [ICompanyEIProjection(year=model_ei_projections[0].year-1, value=model_ei_projections[0].value)] + model_ei_projections
        return model_ei_projections

    def project_ei_targets(self, company: ICompanyData, production_proj: pd.Series, ei_df: pd.DataFrame) -> ICompanyEIProjectionsScopes:
        """Input:
        @company: Company-specific data: target_data and base_year_production
        @production_proj: company's production projection computed from region-sector benchmark growth rates

        If the company has no target or the target can't be processed, then the output the emission database, unprocessed
        If successful, it returns the full set of historic emissions intensities and projections based on targets
        """
        targets = company.target_data
        target_scopes = {t.target_scope for t in targets}
        ei_projection_scopes = {'S1': None, 'S2': None, 'S1S2': None, 'S3': None, 'S1S2S3': None}
        if EScope.S1 in target_scopes and EScope.S2 not in target_scopes and EScope.S1S2 not in target_scopes:
            # We could create an S1S2 target based on S1 and S2 targets, but we don't yet
            # Syntehsize an S2 target using benchmark-aligned data
            s2_ei = asPintSeries(ei_df.loc[EScope.S2])
            s2_netzero_year = s2_ei.idxmin()
            for target in targets:
                if target.target_scope==EScope.S1:
                    s2_target_base_year = max(target.target_base_year,s2_ei.index[0])
                    s2_target_base_m = s2_ei[s2_target_base_year].m
                    if ITR.HAS_UNCERTAINTIES:
                        s2_target_base_err = s2_ei[s2_target_base_year].m
                    else:
                        s2_target_base_err = None
                    s2_target = ITargetData(netzero_year=s2_netzero_year, target_type='intensity', target_scope=EScope.S2,
                                            target_start_year=target.target_start_year,
                                            target_base_year=s2_target_base_year, target_end_year=target.target_end_year,
                                            target_base_year_qty=s2_target_base_m,
                                            target_base_year_err=s2_target_base_err,
                                            target_base_year_unit=str(s2_ei[s2_target_base_year].u),
                                            target_reduction_pct=1.0-(s2_ei[target.target_end_year] / s2_ei[s2_target_base_year]))
                    targets.append(s2_target)
                    
        nz_target_years = self.calculate_nz_target_years(targets)

        for scope_name in ei_projection_scopes:
            netzero_year = nz_target_years[scope_name]
            # If there are no other targets specified (which can happen when we are dealing with inferred netzero targets)
            # target_year and target_ei_value pick up the year and value of the last EI realized
            # Otherwise, they are specified by the targets (intensity or absolute)
            target_year = None
            target_ei_value = None

            scope_targets = [target for target in targets if target.target_scope.name == scope_name]
            no_scope_targets = (scope_targets == [])
            # If we don't have an explicit scope target but we do have an implicit netzero target that applies to this scope,
            # prime the pump for projecting that netzero target, in case we ever need such a projection.  For example,
            # a netzero target for S1+S2 implies netzero targets for both S1 and S2.  The TPI benchmark needs an S1 target
            # for some sectors, and projecting a netzero target for S1 from S1+S2 makes that benchmark useable.
            # Note that we can only infer separate S1 and S2 targets from S1+S2 targets when S1+S2 = 0, because S1=0 + S2=0 is S1+S2=0
            if not scope_targets:
                if company.historic_data is None:
                    # This just defends against poorly constructed test cases
                    nz_target_years[scope_name] = None
                    continue
                if nz_target_years[scope_name]:
                    if (company.projected_intensities is not None
                        and company.projected_intensities[scope_name] is not None
                        and not company.historic_data.emissions_intensities[scope_name]):
                        ei_projection_scopes[scope_name] = company.projected_intensities[scope_name]
                        continue
                    ei_realizations = company.historic_data.emissions_intensities[scope_name]
                    # We can infer a netzero target.  Use our last year historic year of data as the target_year (i.e., target_base_year) value
                    # Due to ragged right edge, we have to hunt.  But we know there's at least one such value.
                    # If there's a proper target for this scope, historic values will be replaced by target values
                    for i in range(len(ei_realizations)-1, -1, -1):
                        target_ei_value = ei_realizations[i].value
                        if ITR.isnan(target_ei_value.m):
                            continue
                        model_ei_projections = self._get_ei_projections_from_ei_realizations(ei_realizations, i)
                        ei_projection_scopes[scope_name] = ICompanyEIProjections(ei_metric=EI_Quantity(f"{target_ei_value.u:~P}"),
                                                                                 projections=self._get_bounded_projections(model_ei_projections))
                        if not ITR.isnan(target_ei_value.m):
                            target_year = ei_realizations[i].year
                            break
                    if target_year is None:
                        # Either no realizations or they are all NaN
                        continue
                    # FIXME: if we have aggressive targets for source of this inference, the inferred
                    # netzero targets may be very slack (because non-netzero targets are not part of the inference)
            scope_targets_intensity = self._order_scope_targets(
                [target for target in scope_targets if target.target_type == "intensity"])
            scope_targets_absolute = self._order_scope_targets(
                [target for target in scope_targets if target.target_type == "absolute"])
            while scope_targets_intensity or scope_targets_absolute:
                if scope_targets_intensity and scope_targets_absolute:
                    target_i = scope_targets_intensity[0]
                    target_a = scope_targets_absolute[0]
                    if target_i.target_end_year == target_a.target_end_year:
                        if target_i.target_start_year >= target_a.target_start_year:
                            if target_i.target_start_year == target_a.target_start_year:
                                warnings.warn(
                                    f"intensity target overrides absolute target for target_start_year={target_i.target_start_year} and target_end_year={target_i.target_end_year}")
                            scope_targets_absolute.pop(0)
                            scope_targets = scope_targets_intensity
                        else:
                            scope_targets_intensity.pop(0)
                            scope_targets = scope_targets_absolute
                    elif target_i.target_end_year < target_a.target_end_year:
                        scope_targets = scope_targets_intensity
                    else:
                        scope_targets = scope_targets_absolute
                elif not scope_targets_intensity:
                    scope_targets = scope_targets_absolute
                else:  # not scope_targets_absolute
                    scope_targets = scope_targets_intensity

                target = scope_targets.pop(0)
                base_year = target.target_base_year
                # Work-around for https://github.com/hgrecco/pint/issues/1687
                target_base_year_unit = ureg.parse_units(target.target_base_year_unit)

                # Put these variables into scope
                last_ei_year = None
                last_ei_value = None

                # Solve for intensity and absolute
                model_ei_projections = None
                if target.target_type == "intensity":
                    # Simple case: the target is in intensity
                    # If target is not the first one for this scope, we continue from last year of the previous target
                    if ei_projection_scopes[scope_name]:
                        (_, last_ei_year), (_, last_ei_value) = ei_projection_scopes[scope_name].projections[-1]
                        last_ei_value = last_ei_value.to(target_base_year_unit)
                        skip_first_year = 1
                    else:
                        # When starting from scratch, use recent historic data if available.
                        if not company.historic_data:
                            ei_realizations = []
                        else:
                            ei_realizations = company.historic_data.emissions_intensities[scope_name]
                        skip_first_year = 0
                        if ei_realizations == []:
                            # Alas, we have no data to align with constituent or containing scope
                            last_ei_year = target.target_base_year
                            target_base_year_m = target.target_base_year_qty
                            if ITR.HAS_UNCERTAINTIES and target.target_base_year_err is not None:
                                target_base_year_m = ITR.ufloat(target_base_year_m, target.target_base_year_err)
                            last_ei_value = Q_(target_base_year_m, target_base_year_unit)
                        else:
                            for i in range(len(ei_realizations)-1, -1, -1):
                                last_ei_year, last_ei_value = ei_realizations[i].year, ei_realizations[i].value
                                if ITR.isnan(last_ei_value.m):
                                    continue
                                model_ei_projections = self._get_ei_projections_from_ei_realizations(ei_realizations, i)
                                ei_projection_scopes[scope_name] = ICompanyEIProjections(ei_metric=EI_Quantity(f"{last_ei_value.u:~P}"),
                                                                                         projections=self._get_bounded_projections(model_ei_projections))
                                skip_first_year = 1
                                break
                            if last_ei_year < target.target_base_year:
                                logger.error(f"Target data for {company.company_id} more up-to-date than disclosed data; please fix and re-run")
                                # breakpoint()
                                raise ValueError
                    target_year = target.target_end_year
                    # Attribute target_reduction_pct of ITargetData is currently a fraction, not a percentage.
                    target_base_year_m = target.target_base_year_qty
                    if ITR.HAS_UNCERTAINTIES and target.target_base_year_err is not None:
                        target_base_year_m = ITR.ufloat(target_base_year_m, target.target_base_year_err)
                    target_ei_value = Q_(target_base_year_m * (1 - target.target_reduction_pct),
                                         target_base_year_unit)
                    if target_ei_value >= last_ei_value:
                        # We've already achieved target, so aim for the next one
                        target_year = last_ei_year
                        target_ei_value = last_ei_value
                        continue
                    CAGR = self._compute_CAGR(last_ei_year, last_ei_value, target_year, target_ei_value)
                    model_ei_projections = [ICompanyEIProjection(year=year, value=CAGR[year])
                                            for year in range(last_ei_year+skip_first_year, 1+target_year)
                                            if year >= self.projection_controls.BASE_YEAR]

                elif target.target_type == "absolute":
                    # Complicated case, the target must be switched from absolute value to intensity.
                    # We use benchmark production data

                    # If target is not the first one for this scope, we continue from last year of the previous target
                    if ei_projection_scopes[scope_name]:
                        (_, last_ei_year), (_, last_ei_value) = ei_projection_scopes[scope_name].projections[-1]
                        last_prod_value = production_proj.loc[last_ei_year]
                        last_em_value = last_ei_value * last_prod_value
                        last_em_value = last_em_value.to(target_base_year_unit)
                        skip_first_year = 1
                    else:
                        if not company.historic_data:
                            em_realizations = []
                        else:
                            em_realizations = company.historic_data.emissions[scope_name]
                        skip_first_year = 0
                        if em_realizations == []:
                            last_ei_year = target.target_base_year
                            target_base_year_m = target.target_base_year_qty
                            if ITR.HAS_UNCERTAINTIES and target.target_base_year_err is not None:
                                target_base_year_m = ITR.ufloat(target_base_year_m, target.target_base_year_err)
                            last_em_value = Q_(target_base_year_m, target_base_year_unit)
                            # FIXME: should be target.base_year_production !!
                            last_prod_value = company.base_year_production
                        else:
                            for i in range(len(em_realizations)-1, -1, -1):
                                last_ei_year, last_em_value = em_realizations[i].year, em_realizations[i].value
                                if ITR.isnan(last_em_value.m):
                                    continue
                                # Just like _get_ei_projections_from_ei_realizations, except these are based on em_realizations, not ei_realizations
                                for j in range(0,i+1):
                                    if em_realizations[j].year >= self.projection_controls.BASE_YEAR and not ITR.isnan(em_realizations[j].value.m):
                                        break
                                model_ei_projections = [ICompanyEIProjection(year=em_realizations[k].year, value=em_realizations[k].value / production_proj.loc[em_realizations[k].year])
                                                        # NaNs in the middle may still be a problem!
                                                        for k in range(j,i+1) if em_realizations[k].year]
                                while model_ei_projections[0].year > self.projection_controls.BASE_YEAR:
                                    model_ei_projections = [ICompanyEIProjection(year=model_ei_projections[0].year-1, value=model_ei_projections[0].value)] + model_ei_projections
                                last_prod_value = production_proj.loc[last_ei_year]
                                ei_projection_scopes[scope_name] = ICompanyEIProjections(ei_metric=EI_Quantity(f"{(last_em_value/last_prod_value).u:~P}"),
                                                                                         projections=self._get_bounded_projections(model_ei_projections))
                                skip_first_year = 1
                                break
                            assert last_ei_year >= target.target_base_year
                        # FIXME: just have to trust that this particular value ties to the first target's year/value pair
                        try:
                            last_ei_value = last_em_value / last_prod_value
                        except UnboundLocalError:
                            logger.error(f"crashed out without finding em_realizations for {company.company_id}")
                            raise

                    target_year = target.target_end_year
                    # Attribute target_reduction_pct of ITargetData is currently a fraction, not a percentage.
                    target_base_year_m = target.target_base_year_qty
                    if ITR.HAS_UNCERTAINTIES and target.target_base_year_err is not None:
                        target_base_year_m = ITR.ufloat(target_base_year_m, target.target_base_year_err)
                    target_em_value = Q_(target_base_year_m * (1 - target.target_reduction_pct),
                                         target_base_year_unit)
                    if target_em_value >= last_em_value:
                        # We've already achieved target, so aim for the next one
                        target_year = last_ei_year
                        target_ei_value = last_ei_value
                        continue
                    CAGR = self._compute_CAGR(last_ei_year, last_em_value, target_year, target_em_value)

                    model_emissions_projections = CAGR.loc[(last_ei_year+skip_first_year):target_year]
                    emissions_projections = model_emissions_projections.astype(f'pint[{target_base_year_unit}]')
                    idx = production_proj.index.intersection(emissions_projections.index)
                    ei_projections = emissions_projections.loc[idx] / production_proj.loc[idx]

                    model_ei_projections = [ICompanyEIProjection(year=year, value=ei_projections[year])
                                            for year in range(last_ei_year+skip_first_year, 1+target_year)
                                            if year >= self.projection_controls.BASE_YEAR]
                    if ei_projection_scopes[scope_name] is None:
                        while model_ei_projections[0].year > self.projection_controls.BASE_YEAR:
                            model_ei_projections = [ICompanyEIProjection(year=model_ei_projections[0].year-1, value=model_ei_projections[0].value)] + model_ei_projections
                else:
                    # No target (type) specified
                    ei_projection_scopes[scope_name] = None
                    continue

                target_ei_value = model_ei_projections[-1].value
                if ei_projection_scopes[scope_name] is not None:
                    ei_projection_scopes[scope_name].projections.extend(model_ei_projections)
                else:
                    while model_ei_projections[0].year > self.projection_controls.BASE_YEAR:
                        model_ei_projections = [ICompanyEIProjection(year=model_ei_projections[0].year-1, value=model_ei_projections[0].value)] + model_ei_projections
                    ei_projection_scopes[scope_name] = ICompanyEIProjections(ei_metric=EI_Quantity (f"{target_ei_value.u:~P}"),
                                                                             projections=self._get_bounded_projections(model_ei_projections))

                if scope_targets_intensity and scope_targets_intensity[0].netzero_year:
                    # Let a later target set the netzero year
                    continue
                if scope_targets_absolute and scope_targets_absolute[0].netzero_year:
                    # Let a later target set the netzero year
                    continue

            # Handle final netzero targets.  Note that any absolute zero target is also zero intensity target (so use target_ei_value)
            # TODO What if target is a 100% reduction.  Does it work whether or not netzero_year is set?
            if netzero_year and netzero_year > target_year:  # add in netzero target at the end
                netzero_qty = Q_(0.0, target_ei_value.u)
                if no_scope_targets and scope_name in ['S1S2S3'] and nz_target_years['S1S2'] <= netzero_year and nz_target_years['S3'] <= netzero_year:
                    if ei_projection_scopes['S1S2'] is None:
                        raise ValueError(f"{company.company_id} is missing S1+S2 historic data for S1+S2 target")
                    if ei_projection_scopes['S3'] is None:
                        raise ValueError(f"{company.company_id} is missing S3 historic data for S3 target")
                    ei_projections = [ei_sum for ei_sum in list(
                        map(ICompanyEIProjection.add, ei_projection_scopes['S1S2'].projections, ei_projection_scopes['S3'].projections) )
                                      if ei_sum.year in range(1 + target_year, 1 + netzero_year)]
                elif no_scope_targets and scope_name in ['S1S2'] and nz_target_years['S1'] <= netzero_year and nz_target_years['S2'] <= netzero_year:
                    if ei_projection_scopes['S1'] is None:
                        raise ValueError(f"{company.company_id} is missing S1 historic data for S1 target")
                    if ei_projection_scopes['S2'] is None:
                        raise ValueError(f"{company.company_id} is missing S2 historic data for S2 target")
                    ei_projections = [ei_sum for ei_sum in list(
                        map(ICompanyEIProjection.add, ei_projection_scopes['S1'].projections, ei_projection_scopes['S2'].projections) )
                                      if ei_sum.year in range(1 + target_year, 1 + netzero_year)]
                else:
                    CAGR = self._compute_CAGR(target_year, target_ei_value, netzero_year, netzero_qty)
                    ei_projections = [ICompanyEIProjection(year=year, value=CAGR[year])
                                      for year in range(1 + target_year, 1 + netzero_year)]
                if ei_projection_scopes[scope_name]:
                    ei_projection_scopes[scope_name].projections.extend(ei_projections)
                else:
                    ei_projection_scopes[scope_name] = ICompanyEIProjections(projections=self._get_bounded_projections(ei_projections),
                                                                             ei_metric=EI_Quantity (f"{target_ei_value.u:~P}"))
                target_year = netzero_year
                target_ei_value = netzero_qty
            if ei_projection_scopes[scope_name] and target_year < ProjectionControls.TARGET_YEAR:
                # Assume everything stays flat until 2050
                ei_projection_scopes[scope_name].projections.extend(
                    [ICompanyEIProjection(year=year, value=target_ei_value)
                     for y, year in enumerate(range(1 + target_year, 1 + ProjectionControls.TARGET_YEAR))]
                )

        # If we are production-centric, S3 and S1S2S3 targets will make their way into S1 and S1S2
        return ICompanyEIProjectionsScopes(**ei_projection_scopes)

    def _compute_CAGR(self, first_year: int, first_value: pint.Quantity, last_year: int, last_value: pint.Quantity) -> pd.Series:
        """Compute CAGR, returning pd.Series of the growth (or reduction) applied to first to converge with last
        :param first_year: the year of the first datapoint in the Calculation (most recent actual datapoint)
        :param first_value: the value of the first datapoint in the Calculation (most recent actual datapoint)
        :param last_year: the year of the final target
        :param last_value: the value of the final target

        :return: pd.Series index by the years from first_year:last_year, with units based on last_value (the target value)
        """

        period = last_year - first_year
        if period <= 0:
            return pd.Series(PA_([], dtype=f"pint[{first_value.u:~P}]"))
        if last_value >= first_value or first_value.m == 0:
            # If we have a slack target, i.e., target goal is actually above current data, clamp so CAGR computes as zero
            return pd.Series(PA_([first_value.m] * (period+1), dtype=f"{first_value.u:~P}"),
                             index=range(first_year, last_year+1),
                             name='CAGR')

        # CAGR doesn't work well with large reductions, so solve with cases:
        CAGR_limit = 1/11.11
        # PintArrays make it easy to convert arrays of magnitudes to types, so ensure magnitude consistency
        first_value = first_value.to(last_value.u)
        if last_value < first_value * CAGR_limit:
            # - If CAGR target > 90% reduction, blend a linear reduction with CAGR to get CAGR-like shape that actually hits the target
            cagr_factor = CAGR_limit ** (1 / period)
            linear_factor = (CAGR_limit * first_value.m - last_value.m)
            cagr_data = [cagr_factor ** y * first_value.m - linear_factor * (y / period)
                         for y, year in enumerate(range(first_year, last_year+1))]
        else:
            if ITR.HAS_UNCERTAINTIES and (isinstance(first_value.m, ITR.UFloat) or isinstance(last_value.m, ITR.UFloat)):
                if isinstance(first_value.m, ITR.UFloat):
                    first_nom = first_value.m.n
                    first_err = first_value.m.s
                else:
                    first_nom = first_value.m
                    first_err = 0.0
                if isinstance(last_value.m, ITR.UFloat):
                    last_nom = last_value.m.n
                    last_err = last_value.m.s
                else:
                    last_nom = last_value.m
                    last_err = 0.0
                cagr_factor_nom = (last_nom / first_nom) ** (1 / period)
                cagr_data = [ITR.ufloat(cagr_factor_nom ** y * first_nom,
                                        first_err * (period-y)/period  + last_err * (y/period))
                             for y, year in enumerate(range(first_year, last_year+1))]
            else:
                # - If CAGR target <= 90% reduction, use CAGR model directly
                cagr_factor = (last_value / first_value).m ** (1 / period)
                cagr_data = [cagr_factor ** y * first_value.m
                             for y, year in enumerate(range(first_year, last_year+1))]
        cagr_result = pd.Series(PA_(cagr_data, dtype=f"{last_value.u:~P}"),
                                index=range(first_year, last_year+1),
                                name='CAGR')
        return cagr_result
